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
