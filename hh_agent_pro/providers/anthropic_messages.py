"""Anthropic Messages 兼容客户端（`POST {base}/v1/messages`），例如公司 LLM 网关上的 Claude。

agent 内部的消息是 OpenAI Chat 风格（见 context_manager.provider_messages）。这里只做两件事：
1. 请求方向：OpenAI 风格 → Anthropic 格式（system 单独放、tool_calls → tool_use 块、tool 消息 → tool_result 块）；
2. 响应方向：text / tool_use 块 → HH 内部协议文本（`<tool>` / `<tool_batch>` / 纯文本）。
重试、退避、错误提示沿用 OpenAIChatClient 的做法；用非流式请求，响应一次拿全，转换更简单。
"""

import json
import time
import urllib.error
import urllib.request

from .base import ModelRequest, model_limits
from .openai_chat import (
    EMPTY_RESPONSE_RETRIES, MAX_ATTEMPTS, RETRYABLE_HTTP_CODES, TRANSIENT_ERRORS, USER_AGENT, OpenAIChatClient, merge_usage,
)

ANTHROPIC_VERSION = "2023-06-01"


def to_anthropic_tools(tools):
    return [
        {
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "input_schema": tool["function"].get("parameters") or {"type": "object", "properties": {}},
        }
        for tool in tools or []
        if tool.get("type") == "function"
    ]


def to_anthropic_messages(messages):
    """OpenAI 风格消息 → Anthropic messages。连续同角色的消息合并成一条，工具结果放在该条最前面。"""
    converted = []

    def add(role, blocks):
        blocks = [b for b in blocks if b.get("type") != "text" or b.get("text")]  # 空文本块会被拒绝
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            merged = converted[-1]["content"] + blocks
            # tool_result 必须排在 user 内容的最前面
            merged.sort(key=lambda b: 0 if b.get("type") == "tool_result" else 1)
            converted[-1]["content"] = merged
        else:
            converted.append({"role": role, "content": blocks})

    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": str(message.get("tool_call_id", "")), "content": str(content)}])
        elif role == "assistant":
            blocks = [{"type": "text", "text": str(content)}]
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append({"type": "tool_use", "id": str(call.get("id", "")), "name": str(function.get("name", "")), "input": arguments})
            add("assistant", blocks)
        elif role == "user":
            add("user", [{"type": "text", "text": str(content)}])
    return converted


def usage_details(usage):
    """input_tokens 不含缓存部分；这里把缓存读写也算进输入，和 OpenAI 的 prompt_tokens 口径一致（预算统计偏保守）。"""
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0) + cache_read + cache_write
    output_tokens = int(usage.get("output_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cached_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cache_hit": cache_read > 0,
    }


class AnthropicMessagesClient:
    def __init__(self, model, base_url, api_key, temperature=None, timeout=300, context_window=None, label="model"):
        self.model = model
        self.base_url = str(base_url).rstrip("/").removesuffix("/v1")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.label = label
        limits = model_limits(model, context_window)
        self.context_window = limits["context_window"]
        self.max_output_tokens = limits["max_output_tokens"]
        self.requires_reasoning_content_for_tool_calls = False
        self.last_completion_metadata = {}
        self.last_native_assistant_message = {}
        self.sleep = time.sleep  # 测试里替换掉，避免真的等待

    def _post(self, payload, callback):
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request_body = json.dumps(payload).encode("utf-8")  # 别叫 body：下面 HTTPError 分支用 body 存错误响应
        for attempt in range(1, MAX_ATTEMPTS + 1):
            # 每次重试都新建 Request：走代理时复用同一个对象会让重试变成 CONNECT host:80（见 openai_chat._post）
            http_request = urllib.request.Request(
                self.base_url + "/v1/messages", data=request_body, headers=headers, method="POST",
            )
            try:
                with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code not in RETRYABLE_HTTP_CODES or attempt == MAX_ATTEMPTS:
                    hint = " Check the API key for this provider." if exc.code == 401 else ""
                    raise RuntimeError(f"Anthropic-messages request failed with HTTP {exc.code}: {body}{hint}") from exc
                delay = OpenAIChatClient._retry_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None)
                reason = f"HTTP {exc.code}"
            except TRANSIENT_ERRORS as exc:
                if attempt == MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Could not reach the Anthropic-messages backend after {MAX_ATTEMPTS} attempts.\n"
                        f"Base URL: {self.base_url}\nModel: {self.model}\nLast error: {exc}"
                    ) from exc
                delay = OpenAIChatClient._retry_delay(attempt)
                reason = exc.__class__.__name__
            if callback:
                callback({
                    "type": "provider_status",
                    "message": f"{self.label} request failed ({reason}); retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s",
                })
            self.sleep(delay)

    def complete(self, request: ModelRequest):
        self.last_completion_metadata = {}
        self.last_native_assistant_message = {}
        payload = {
            "model": self.model,
            "system": request.system_prompt,
            "messages": to_anthropic_messages(request.messages),
            "max_tokens": int(request.max_new_tokens),
        }
        if request.tools:
            payload["tools"] = to_anthropic_tools(request.tools)
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        callback = request.event_callback
        if callback:
            callback({"type": "provider_status", "message": f"{self.label} turn started"})
        usage_total = {}
        for retry in range(EMPTY_RESPONSE_RETRIES + 1):
            response = self._post(payload, callback)
            usage_total = merge_usage(usage_total, usage_details(response.get("usage") or {}))
            blocks = response.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [b for b in blocks if b.get("type") == "tool_use"]
            stop_reason = str(response.get("stop_reason") or "")
            # 空响应原样重发（同 OpenAIChatClient）；空但被截断（max_tokens）交给 AgentLoop 的截断恢复
            if text or calls or stop_reason == "max_tokens" or retry == EMPTY_RESPONSE_RETRIES:
                break
            if callback:
                callback({"type": "provider_status",
                          "message": f"{self.label} returned an empty response (stop_reason={stop_reason or 'none'}); "
                                     f"resending {retry + 1}/{EMPTY_RESPONSE_RETRIES}"})
        if callback:
            callback({"type": "provider_status", "message": f"{self.label} turn completed"})

        self.last_completion_metadata = {
            "finish_reason": stop_reason,
            "output_truncated": stop_reason == "max_tokens",
            "max_output_tokens_requested": int(request.max_new_tokens),
            "native_tool_calls_returned": len(calls),
            "empty_response_retries": retry,
            **usage_total,
        }
        if not calls:
            if text or stop_reason == "max_tokens":
                return text
            raise RuntimeError(
                "Anthropic-messages error: could not extract text from response "
                f"(stop_reason={stop_reason or 'none'}, after {retry} resend(s))"
            )

        self.last_native_assistant_message = {
            "tool_call_id": str(calls[0].get("id", "")),
            "content": text or None,
            "reasoning_content": "",
        }
        actions = [{"id": str(c.get("id", "")), "name": c.get("name", ""), "args": c.get("input") or {}} for c in calls]
        if len(actions) == 1:
            return "<tool>" + json.dumps({"name": actions[0]["name"], "args": actions[0]["args"]}, ensure_ascii=False) + "</tool>"
        self.last_native_assistant_message["tool_calls"] = actions
        return "<tool_batch>" + json.dumps(actions, ensure_ascii=False) + "</tool_batch>"
