"""OpenAI Chat Completions 兼容客户端。

DeepSeek、通义千问兼容模式、OpenAI 本身等都实现了 `/chat/completions`，
所以只需要这一个客户端，差别只在 base_url、model 和 api_key。

流式读取 SSE：增量里的文本可能把 HH 的标签拆开，所以只在流上展示进度，
完整文本和原生 tool_calls 收齐后再转成内部协议交给 AgentLoop。
"""

import json
import random
import socket
import time
import urllib.error
import urllib.request
from http.client import IncompleteRead, RemoteDisconnected

from .base import ModelRequest, model_limits

USER_AGENT = "hh/0.1"
# 这些模型在 thinking 模式下返回 tool_calls 时，下一轮必须原样回传 reasoning_content。
REASONING_REPLAY_MODEL_MARKERS = ("deepseek-v4", "deepseek-reasoner", "deepseek-flash")

# 限流、服务端过载、网络抖动都是暂时性的：指数退避加抖动后重试，优先听从 Retry-After。
# 400/401/402/403/404 是请求或账户本身的问题，重试没有意义，直接报错。
RETRYABLE_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_ATTEMPTS = 6
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 60.0
TRANSIENT_ERRORS = (urllib.error.URLError, RemoteDisconnected, IncompleteRead, ConnectionError, socket.timeout, TimeoutError)
# thinking 模式偶尔只返回推理、没有正文也没有工具调用（SWE-bench django-12406，整题因此失败）。
# 请求没有副作用、输出有随机性，原样重发通常就能拿到正常结果；重发几次仍为空才报错。
EMPTY_RESPONSE_RETRIES = 2


def merge_usage(total, details):
    """把多次请求（含空响应重试）的用量累加，预算统计不漏算。"""
    merged = dict(details)
    for key in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        merged[key] = int(total.get(key) or 0) + int(details.get(key) or 0)
    merged["cache_hit"] = merged.get("cached_tokens", 0) > 0
    return merged


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _usage_details(event):
    usage = event.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    # DeepSeek 用 prompt_cache_hit_tokens，OpenAI 用 prompt_tokens_details.cached_tokens。
    cached = int(details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached,
        "cache_hit": cached > 0,
    }


class OpenAIChatClient:
    def __init__(self, model, base_url, api_key, temperature=None, timeout=300, context_window=None, label="model"):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.label = label
        limits = model_limits(model, context_window)
        self.context_window = limits["context_window"]
        self.max_output_tokens = limits["max_output_tokens"]
        self.requires_reasoning_content_for_tool_calls = any(
            marker in str(model).casefold() for marker in REASONING_REPLAY_MODEL_MARKERS
        )
        self.last_completion_metadata = {}
        self.sleep = time.sleep  # 测试里替换掉，避免真的等待
        # thinking 模式的 tool call 需要随 assistant 消息回传的协议状态；
        # 与 completion metadata 分开存放，私有推理不会进入 trace/report。
        self.last_native_assistant_message = {}

    def _post(self, payload, callback):
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": USER_AGENT}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request_body = json.dumps(payload).encode("utf-8")  # 别叫 body：下面 HTTPError 分支用 body 存错误响应
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # 每次重试都新建 Request：走代理时 urllib 会在 Request 上原地设置隧道，复用同一个对象的第二次请求
            # 会退化成 type=http、隧道目标不带端口，变成 CONNECT host:80，于是第一次失败之后的重试全部失败
            # （SWE-bench sympy-13551 等：503 之后的重试都被出口代理以 403 拒绝）。
            http_request = urllib.request.Request(
                self.base_url + "/chat/completions", data=request_body, headers=headers, method="POST",
            )
            try:
                # 流读到一半断开也走同一个重试：请求没有副作用，整次重发即可。
                with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                    return self._read_stream(response, callback)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code not in RETRYABLE_HTTP_CODES or attempt == MAX_ATTEMPTS:
                    hint = " Check the API key for this provider." if exc.code == 401 else ""
                    raise RuntimeError(f"OpenAI-chat-compatible request failed with HTTP {exc.code}: {body}{hint}") from exc
                delay = self._retry_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None)
                reason = f"HTTP {exc.code}"
            except TRANSIENT_ERRORS as exc:
                if attempt == MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Could not reach the OpenAI-chat-compatible backend after {MAX_ATTEMPTS} attempts.\n"
                        f"Base URL: {self.base_url}\nModel: {self.model}\nLast error: {exc}"
                    ) from exc
                delay = self._retry_delay(attempt)
                reason = exc.__class__.__name__
            if callback:
                callback({
                    "type": "provider_status",
                    "message": f"{self.label} request failed ({reason}); retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s",
                })
            self.sleep(delay)

    @staticmethod
    def _retry_delay(attempt, retry_after=None):
        if retry_after:
            try:
                return min(MAX_DELAY_SECONDS, max(0.0, float(retry_after)))
            except ValueError:
                pass  # HTTP-date 形式的 Retry-After 较少见，退回指数退避
        backoff = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** (attempt - 1))
        return backoff * (0.5 + random.random() / 2)

    def _read_stream(self, response, callback):
        text_parts, reasoning_parts, tool_calls = [], [], {}
        usage, finish_reason = {}, ""
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload_text = line[len("data:"):].strip()
            if not payload_text or payload_text == "[DONE]":
                continue
            try:
                event = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            if event.get("usage"):
                usage = event
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason") is not None:
                finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or {}
            for tool_delta in delta.get("tool_calls") or []:
                call = tool_calls.setdefault(int(tool_delta.get("index", 0)), {"id": "", "name": "", "arguments": []})
                if tool_delta.get("id"):
                    call["id"] = str(tool_delta["id"])
                function = tool_delta.get("function") or {}
                if function.get("name"):
                    call["name"] = str(function["name"])
                if function.get("arguments"):
                    call["arguments"].append(str(function["arguments"]))
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                if not reasoning_parts and callback:
                    callback({"type": "provider_status", "message": f"{self.label} reasoning in progress"})
                reasoning_parts.append(reasoning)
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
        return "".join(text_parts), "".join(reasoning_parts), tool_calls, usage, finish_reason

    def complete(self, request: ModelRequest):
        self.last_completion_metadata = {}
        self.last_native_assistant_message = {}
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system_prompt}, *request.messages],
            "max_tokens": int(request.max_new_tokens),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = list(request.tools)
            payload["tool_choice"] = "auto"
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        callback = request.event_callback
        if callback:
            callback({"type": "provider_status", "message": f"{self.label} turn started"})
        usage_total = {}
        for retry in range(EMPTY_RESPONSE_RETRIES + 1):
            body_text, reasoning, tool_calls, usage, finish_reason = self._post(payload, callback)
            usage_total = merge_usage(usage_total, _usage_details(usage))
            # 空但被截断（finish_reason=length）不重发：交给 AgentLoop 的截断恢复
            if body_text or tool_calls or finish_reason == "length" or retry == EMPTY_RESPONSE_RETRIES:
                break
            if callback:
                callback({"type": "provider_status",
                          "message": f"{self.label} returned an empty response (finish_reason={finish_reason or 'none'}); "
                                     f"resending {retry + 1}/{EMPTY_RESPONSE_RETRIES}"})
        if callback:
            callback({"type": "provider_status", "message": f"{self.label} turn completed"})

        # 思考内容不进 UI，但写进 run 的完整对话记录（transcript.jsonl），复盘时要知道模型为什么这么做。
        self.last_reasoning_content = reasoning
        self.last_completion_metadata = {
            "finish_reason": finish_reason,
            "output_truncated": finish_reason == "length",
            "max_output_tokens_requested": int(request.max_new_tokens),
            "native_tool_calls_returned": len(tool_calls),
            "empty_response_retries": retry,
            **usage_total,
        }
        # 被长度截断的 tool call 往往是半截 JSON：交回 AgentLoop 走截断恢复，而不是报成模型错误。
        if finish_reason == "length" and tool_calls:
            return body_text
        if not tool_calls:
            if body_text or finish_reason == "length":
                return body_text
            raise RuntimeError(
                "OpenAI-chat-compatible error: could not extract text from response "
                f"(finish_reason={finish_reason or 'none'}, after {retry} resend(s))"
            )

        calls = [tool_calls[index] for index in sorted(tool_calls)]
        # reasoning_content 必须原样回传，只用于协议重放，不进入 UI/trace。
        self.last_native_assistant_message = {
            "tool_call_id": str(calls[0].get("id", "")),
            "content": body_text or None,
            "reasoning_content": reasoning,
        }
        actions = []
        for call in calls:
            raw_arguments = "".join(call["arguments"]) or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                # 参数是半截 JSON：原来直接抛错，整轮以 model_error 结束（2026-11-05 故障注入发现）。
                # 改成交回一个同样解析不了的文本工具调用，AgentLoop 走"格式错了、让模型重来"那条路；
                # 清掉原生协议状态，这次回复不进历史，不会留下没有工具结果的 tool_call id（真实 API 会报 400）。
                self.last_native_assistant_message = {}
                self.last_completion_metadata["malformed_native_tool_arguments"] = True
                return "<tool>" + json.dumps({"name": call["name"]})[:-1] + ', "args": ' + raw_arguments + "}</tool>"
            actions.append({"id": str(call.get("id", "")), "name": call["name"], "args": arguments})
        if len(actions) == 1:
            return "<tool>" + json.dumps({"name": actions[0]["name"], "args": actions[0]["args"]}, ensure_ascii=False) + "</tool>"
        self.last_native_assistant_message["tool_calls"] = actions
        return "<tool_batch>" + json.dumps(actions, ensure_ascii=False) + "</tool_batch>"
