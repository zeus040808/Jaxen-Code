"""模型请求的退避重试：暂时性错误重试，账户/请求本身的错误直接失败。

问题：原先只对 5xx 重试 3 次、间隔 0.5 秒；429 限流、读超时、流读到一半断开都会让整轮任务失败。
"""

import io
import urllib.error

import pytest

from hh_agent_pro.providers import ModelRequest, OpenAIChatClient
from hh_agent_pro.providers.openai_chat import MAX_ATTEMPTS

OK_STREAM = [b'data: {"choices":[{"delta":{"content":"<final>ok</final>"}}]}\n', b"data: [DONE]\n"]


class Response:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return iter(self.lines)

    def __exit__(self, *exc):
        return False


class BrokenStream:
    """流读到一半断开。"""

    def __enter__(self):
        def lines():
            yield OK_STREAM[0][:20]
            raise ConnectionResetError("connection reset by peer")
        return lines()

    def __exit__(self, *exc):
        return False


def _http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://x/v1/chat/completions", code, "err", headers, io.BytesIO(b'{"error":"x"}'))


def _client(monkeypatch, outcomes):
    calls = []

    def fake_open(request, timeout):
        outcome = outcomes[len(calls)]
        calls.append(outcome)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("hh_agent_pro.providers.openai_chat.urllib.request.urlopen", fake_open)
    client = OpenAIChatClient("deepseek-flash", "https://api.deepseek.com", "key", 0.2, 5, label="DeepSeek")
    delays = []
    client.sleep = delays.append
    return client, calls, delays


def _request(events=None):
    return ModelRequest("rules", [{"role": "user", "content": "hi"}], 64, event_callback=(events.append if events is not None else None))


def test_retry_behind_proxy_keeps_the_https_tunnel(monkeypatch):
    # 走代理时 urllib 的 ProxyHandler 会在 Request 上原地 set_proxy；复用同一个 Request 重试，
    # 第二次就退化成 type=http、隧道不带端口（CONNECT api.deepseek.com:80），被出口代理拒绝。
    # SWE-bench sympy-13551、scikit-learn-25747：DeepSeek 一次 503 之后，后面的重试全部失败。
    seen = []

    def proxied_open(request, timeout):
        request.set_proxy("hh-eval-proxy:8888", "http")  # 模拟 ProxyHandler 对 https 请求做的事
        # 真实的 urlopen 要求请求体是 bytes（第一版修复把它覆盖成了错误响应的 str，重试时 TypeError）
        assert isinstance(request.data, bytes)
        seen.append((request.type, request.selector))
        if len(seen) == 1:
            raise _http_error(503)
        return Response(OK_STREAM)

    monkeypatch.setattr("hh_agent_pro.providers.openai_chat.urllib.request.urlopen", proxied_open)
    client = OpenAIChatClient("deepseek-flash", "https://api.deepseek.com", "key", 0.2, 5, label="DeepSeek")
    client.sleep = lambda delay: None

    assert client.complete(_request()) == "<final>ok</final>"
    # 重试仍是 https 隧道（selector 是路径），不是退化后的 http 转发（selector 变成完整 URL）
    assert [kind for kind, _ in seen] == ["https", "https"]
    assert all(selector.startswith("/") for _, selector in seen)


@pytest.mark.parametrize("failure", [_http_error(429), _http_error(503), TimeoutError("read timed out"), urllib.error.URLError("dns")])
def test_transient_failures_are_retried(monkeypatch, failure):
    client, calls, delays = _client(monkeypatch, [failure, Response(OK_STREAM)])

    assert client.complete(_request()) == "<final>ok</final>"
    assert len(calls) == 2
    assert len(delays) == 1


def test_stream_broken_midway_is_retried(monkeypatch):
    client, calls, _ = _client(monkeypatch, [BrokenStream(), Response(OK_STREAM)])

    assert client.complete(_request()) == "<final>ok</final>"
    assert len(calls) == 2


@pytest.mark.parametrize("code", [400, 401, 402, 403, 404])
def test_request_or_account_errors_fail_immediately(monkeypatch, code):
    client, calls, delays = _client(monkeypatch, [_http_error(code)])

    with pytest.raises(RuntimeError, match=f"HTTP {code}"):
        client.complete(_request())
    assert len(calls) == 1 and delays == []


def test_retry_after_header_is_honored(monkeypatch):
    client, _, delays = _client(monkeypatch, [_http_error(429, retry_after="7"), Response(OK_STREAM)])

    client.complete(_request())

    assert delays == [7.0]


def test_backoff_grows_and_gives_up_after_max_attempts(monkeypatch):
    client, calls, delays = _client(monkeypatch, [_http_error(503)] * MAX_ATTEMPTS)
    events = []

    with pytest.raises(RuntimeError, match="HTTP 503"):
        client.complete(_request(events))

    assert len(calls) == MAX_ATTEMPTS
    assert len(delays) == MAX_ATTEMPTS - 1
    assert delays[-1] > delays[0]  # 指数退避（带抖动，仍单调变大）
    assert any("retry 1/" in event["message"] for event in events if event["type"] == "provider_status")


# ---- 空响应：只有推理、没有正文也没有工具调用 ----------------------------------
# SWE-bench django-12406：DeepSeek 第 18 轮只返回 reasoning_content，provider 报
# "could not extract text"，整题失败，前面 65 万 token 的进度作废。

def _stream(content=None, reasoning="thinking...", finish="stop", usage=(100, 10)):
    import json as _json
    delta = {"reasoning_content": reasoning}
    if content:
        delta["content"] = content
    return Response([
        ("data: " + _json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n").encode(),
        ("data: " + _json.dumps({"choices": [], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}) + "\n").encode(),
        b"data: [DONE]\n",
    ])


def test_empty_response_is_resent_and_usage_includes_retries(monkeypatch):
    client, calls, _ = _client(monkeypatch, [_stream(), _stream(content="<final>ok</final>")])
    assert client.complete(ModelRequest("s", [{"role": "user", "content": "go"}], 100)) == "<final>ok</final>"
    assert len(calls) == 2
    assert client.last_completion_metadata["input_tokens"] == 200  # 重试那次的用量也算进去
    assert client.last_completion_metadata["empty_response_retries"] == 1


def test_persistently_empty_response_fails_with_finish_reason(monkeypatch):
    from hh_agent_pro.providers.openai_chat import EMPTY_RESPONSE_RETRIES

    client, calls, _ = _client(monkeypatch, [_stream()] * (EMPTY_RESPONSE_RETRIES + 1))
    with pytest.raises(RuntimeError, match="finish_reason=stop"):
        client.complete(ModelRequest("s", [{"role": "user", "content": "go"}], 100))
    assert len(calls) == EMPTY_RESPONSE_RETRIES + 1


def test_empty_but_truncated_response_goes_to_truncation_recovery(monkeypatch):
    client, calls, _ = _client(monkeypatch, [_stream(finish="length")])
    assert client.complete(ModelRequest("s", [{"role": "user", "content": "go"}], 100)) == ""
    assert client.last_completion_metadata["output_truncated"] is True and len(calls) == 1


def test_malformed_native_tool_arguments_become_a_retry_not_a_model_error(monkeypatch):
    # 2026-11-05 故障注入发现：原生 tool_calls 的参数是半截 JSON 时 provider 直接抛错，整轮以 model_error 结束。
    # 现在交回一个解析不了的文本工具调用，AgentLoop 让模型重来；原生协议状态清空，不留下没有结果的 tool_call id。
    import json as _json
    from hh_agent_pro.agent import CodingAgent

    call = {"index": 0, "id": "call_1", "function": {"name": "patch_file", "arguments": '{"path": "calc.py",'}}
    stream = Response([
        ("data: " + _json.dumps({"choices": [{"delta": {"reasoning_content": "t", "tool_calls": [call]},
                                              "finish_reason": "tool_calls"}]}) + "\n").encode(),
        b"data: [DONE]\n",
    ])
    client, calls, _ = _client(monkeypatch, [stream])
    raw = client.complete(ModelRequest("s", [{"role": "user", "content": "go"}], 100))

    kind, notice = CodingAgent.parse(raw)
    assert kind == "retry" and "malformed tool JSON" in notice
    assert client.last_native_assistant_message == {}
    assert client.last_completion_metadata["malformed_native_tool_arguments"] is True
    assert len(calls) == 1  # 不重发请求：让模型在下一轮按纠正提示重来
