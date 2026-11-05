"""Anthropic Messages 兼容 provider：OpenAI 风格消息 ↔ Anthropic 格式的转换、用量口径、重试（不访问网络）。"""

import io
import json
import urllib.error
from unittest.mock import patch

from jaxen.providers import AnthropicMessagesClient, ModelRequest
from jaxen.providers.anthropic_messages import to_anthropic_messages, to_anthropic_tools, usage_details


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(content, stop="end_turn", usage=None):
    body = {"content": content, "stop_reason": stop,
            "usage": usage or {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90}}
    return FakeResponse(json.dumps(body).encode())


def test_messages_are_converted_and_tool_results_follow_their_calls():
    messages = [
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "x = 1"},
        {"role": "user", "content": "<system-reminder>keep going</system-reminder>"},
    ]
    converted = to_anthropic_messages(messages)
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]  # 连续的 user 合并成一条
    assert converted[1]["content"] == [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}}]  # 空文本块被去掉
    assert converted[2]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "x = 1"}  # 工具结果在最前
    assert converted[2]["content"][1]["text"].startswith("<system-reminder>")


def test_tools_use_input_schema():
    tools = [{"type": "function", "function": {"name": "search", "description": "find", "parameters": {"type": "object"}}}]
    assert to_anthropic_tools(tools) == [{"name": "search", "description": "find", "input_schema": {"type": "object"}}]


def test_usage_counts_cache_reads_as_input():
    details = usage_details({"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 3})
    assert details["input_tokens"] == 103 and details["cached_tokens"] == 90 and details["cache_write_tokens"] == 3


def _client():
    client = AnthropicMessagesClient("Claude-Opus-4.8-joybuilder", "http://gw.local/anthropic", "key")
    client.sleep = lambda _: None
    return client


def test_single_tool_use_becomes_internal_tool_call_and_request_is_anthropic_shaped():
    client = _client()
    sent = {}

    def fake_urlopen(request, timeout):
        sent["url"], sent["headers"], sent["body"] = request.full_url, dict(request.headers), json.loads(request.data)
        return reply([{"type": "text", "text": "Let me read it."},
                      {"type": "tool_use", "id": "t9", "name": "read_file", "input": {"path": "a.py"}}], stop="tool_use")

    with patch("jaxen.providers.anthropic_messages.urllib.request.urlopen", fake_urlopen):
        out = client.complete(ModelRequest("rules", [{"role": "user", "content": "go"}], 1000,
                                           tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}]))
    assert out == '<tool>{"name": "read_file", "args": {"path": "a.py"}}</tool>'
    assert sent["url"] == "http://gw.local/anthropic/v1/messages"
    assert sent["headers"]["Authorization"] == "Bearer key" and sent["headers"]["Anthropic-version"] == "2023-06-01"
    assert sent["body"]["system"] == "rules" and sent["body"]["tools"][0]["name"] == "read_file"
    assert client.last_native_assistant_message["tool_call_id"] == "t9"
    assert client.last_completion_metadata["input_tokens"] == 100 and client.last_completion_metadata["cached_tokens"] == 90


def test_multiple_tool_uses_become_a_batch_and_text_passes_through():
    client = _client()
    two_calls = reply([{"type": "tool_use", "id": "a", "name": "read_file", "input": {"path": "x"}},
                       {"type": "tool_use", "id": "b", "name": "read_file", "input": {"path": "y"}}], stop="tool_use")
    with patch("jaxen.providers.anthropic_messages.urllib.request.urlopen", lambda r, timeout: two_calls):
        out = client.complete(ModelRequest("s", [{"role": "user", "content": "go"}], 100))
    assert out.startswith("<tool_batch>") and [a["id"] for a in client.last_native_assistant_message["tool_calls"]] == ["a", "b"]

    with patch("jaxen.providers.anthropic_messages.urllib.request.urlopen",
               lambda r, timeout: reply([{"type": "text", "text": "<final>done</final>"}])):
        assert client.complete(ModelRequest("s", [{"role": "user", "content": "go"}], 100)) == "<final>done</final>"


def test_rate_limit_is_retried():
    client = _client()
    calls = {"n": 0}

    def flaky(request, timeout):
        calls["n"] += 1
        # 每次重试都是新的 Request，请求体必须仍是 bytes（真实 urlopen 的要求）
        assert isinstance(request.data, bytes)
        if calls["n"] == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "busy", {}, io.BytesIO(b"slow down"))
        return reply([{"type": "text", "text": "ok"}])

    with patch("jaxen.providers.anthropic_messages.urllib.request.urlopen", flaky):
        assert client.complete(ModelRequest("s", [{"role": "user", "content": "go"}], 100)) == "ok"
    assert calls["n"] == 2


def test_empty_response_is_resent_then_succeeds():
    client = _client()
    replies = [reply([{"type": "thinking", "thinking": "..."}]), reply([{"type": "text", "text": "<final>ok</final>"}])]
    with patch("jaxen.providers.anthropic_messages.urllib.request.urlopen", lambda r, timeout: replies.pop(0)):
        assert client.complete(ModelRequest("s", [{"role": "user", "content": "go"}], 100)) == "<final>ok</final>"
    assert client.last_completion_metadata["input_tokens"] == 200 and client.last_completion_metadata["empty_response_retries"] == 1


def test_empty_but_max_tokens_goes_to_truncation_recovery():
    client = _client()
    with patch("jaxen.providers.anthropic_messages.urllib.request.urlopen",
               lambda r, timeout: reply([], stop="max_tokens")):
        assert client.complete(ModelRequest("s", [{"role": "user", "content": "go"}], 100)) == ""
    assert client.last_completion_metadata["output_truncated"] is True
