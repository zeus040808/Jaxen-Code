import json
import os

import pytest

from hh_agent_pro.providers import ModelRequest, OpenAIChatClient
from hh_agent_pro.run_state import (
    STOP_REASON_INTERRUPTED,
    STOP_REASON_MODEL_PROTOCOL_ERROR,
    STOP_REASON_OUTPUT_TRUNCATED,
)
from hh_agent_pro.agent import CodingAgent
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.streaming import CallbackEventSink
from hh_agent_pro.tools import provider_tool_schemas, shell_command_is_read_only
from hh_agent_pro.workspace import WorkspaceContext


class ScriptedModel:
    provider = "test"
    model = "scripted"
    last_completion_metadata = {}

    def __init__(self, outputs=("<final>ok</final>",)):
        self.outputs = list(outputs)

    def complete(self, request):

        return self.outputs.pop(0)


def make_agent(tmp_path, model=None, approval="auto", event_sink=None):
    return CodingAgent(
        model_client=model or ScriptedModel(),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".hh" / "sessions"),
        approval_policy=approval,
        max_steps=6,
        event_sink=event_sink,
    )


def test_runtime_info_is_redacted_and_does_not_read_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("HH_DEEPSEEK_API_KEY", "secret-value-that-must-not-appear")
    agent = make_agent(tmp_path)

    result = agent.run_tool("runtime_info", {})

    assert "provider: test" in result
    assert "model: scripted" in result
    assert "repo_root:" in result
    assert "secret-value-that-must-not-appear" not in result


def test_list_dir_alias_uses_safe_list_files_tool(tmp_path):
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    agent = make_agent(tmp_path, approval="never")

    result = agent.execute_tool("list_dir", {"path": "."})

    assert result.metadata["tool_status"] == "ok"
    assert "sample.py" in result.content


def test_read_files_reads_multiple_utf8_files_in_one_safe_call(tmp_path):
    (tmp_path / "a.py").write_text("变量 = '甲'\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("变量 = '乙'\n", encoding="utf-8")
    agent = make_agent(tmp_path, approval="never")

    result = agent.execute_tool("read_files", {"paths": ["a.py", "b.py"]})

    assert result.metadata["tool_status"] == "ok"
    assert "甲" in result.content and "乙" in result.content
    assert agent.memory.to_dict()["recent_files"] == ["a.py", "b.py"]


def test_read_files_creates_per_file_structural_evidence(tmp_path):
    (tmp_path / "store.py").write_text(
        '"""Persistent memory records."""\n\nclass MemoryStore:\n    pass\n\ndef load_memory():\n    return []\n',
        encoding="utf-8",
    )
    (tmp_path / "working.py").write_text(
        '"""Current task working set."""\n\nclass WorkingMemory:\n    pass\n\ndef render_context():\n    return ""\n',
        encoding="utf-8",
    )
    agent = make_agent(tmp_path, approval="never")

    result = agent.execute_tool("read_files", {"paths": ["store.py", "working.py"], "start": 1, "end": 300})

    assert result.metadata["tool_status"] == "ok"
    assert "classes=MemoryStore" in agent.source_evidence.file_summary("store.py")
    assert "functions=render_context" in agent.source_evidence.file_summary("working.py")


def test_unchanged_read_uses_compact_cache_and_file_change_invalidates_it(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text("def first():\n    return 1\n", encoding="utf-8")
    agent = make_agent(tmp_path, approval="never")

    first = agent.execute_tool("read_file", {"path": "sample.py"})
    cached = agent.execute_tool("read_file", {"path": "sample.py"})
    source.write_text("def second():\n    return 2\n", encoding="utf-8")
    refreshed = agent.execute_tool("read_file", {"path": "sample.py"})

    assert first.metadata["tool_status"] == "ok"
    assert cached.metadata["cache_hit"] is True
    assert cached.metadata["effective_tool_step"] is False
    assert "cached observation" in cached.content
    assert not refreshed.metadata.get("cache_hit", False)
    assert "functions=second" in agent.source_evidence.file_summary("sample.py")


def test_shell_utf8_bytes_are_decoded_without_gbk_crash(tmp_path):
    agent = make_agent(tmp_path, approval="auto")
    command = f'"{os.sys.executable}" -c "print(chr(20013)+chr(25991))"'

    result = agent.execute_tool("run_shell", {"command": command})

    assert result.metadata["tool_status"] == "ok"
    assert "中文" in result.content


def test_shell_read_only_classification_is_narrow():
    assert shell_command_is_read_only("dir /b")
    assert shell_command_is_read_only("type README.md")
    assert shell_command_is_read_only("git status")
    assert not shell_command_is_read_only("echo text")
    assert not shell_command_is_read_only("type README.md > copy.txt")
    assert not shell_command_is_read_only("del README.md")


def test_safe_task_tool_does_not_accidentally_require_approval(tmp_path):
    agent = make_agent(tmp_path, approval="never")
    result = agent.execute_tool("list_tasks", {})
    assert result.metadata["tool_status"] == "ok"


def test_deepseek_native_tool_schema_and_streamed_tool_call(monkeypatch):
    class Response:
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"inspect "}}]}\n',
            b'data: {"choices":[{"delta":{"reasoning_content":"both files"}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_provider_1","function":{"name":"read_files","arguments":"{\\"paths\\":[\\"a.py\\","}}]}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"b.py\\"]}"}}]}}]}\n',
            b'data: [DONE]\n',
        ]

        def __enter__(self):
            return iter(self.lines)

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(
        "hh_agent_pro.providers.openai_chat.urllib.request.urlopen",
        lambda request, timeout: Response(),
    )
    client = OpenAIChatClient(
        "deepseek-chat", "https://api.deepseek.com", "key", 0.2, 5
    )
    schemas = provider_tool_schemas(
        {"read_files": {"schema": {"paths": "list[str]"}, "description": "read", "risky": False}}
    )

    result = client.complete(ModelRequest("rules", [{"role": "user", "content": "prompt"}], 100, tools=schemas))

    assert json.loads(result.removeprefix("<tool>").removesuffix("</tool>")) == {
        "name": "read_files",
        "args": {"paths": ["a.py", "b.py"]},
    }
    assert client.last_native_assistant_message == {
        "tool_call_id": "call_provider_1",
        "content": None,
        "reasoning_content": "inspect both files",
    }


def test_deepseek_thinking_tool_turn_replays_exact_reasoning_content(monkeypatch, tmp_path):
    requests = []

    class Response:
        def __init__(self, lines):
            self.lines = lines

        def __enter__(self):
            return iter(self.lines)

        def __exit__(self, exc_type, exc, traceback):
            return False

    responses = [
        Response(
            [
                b'data: {"choices":[{"delta":{"reasoning_content":"private-part-1 "}}]}\n',
                b'data: {"choices":[{"delta":{"reasoning_content":"private-part-2"}}]}\n',
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_ds_42","function":{"name":"list_files","arguments":"{\\"path\\":\\".\\"}"}}]},"finish_reason":"tool_calls"}]}\n',
                b'data: [DONE]\n',
            ]
        ),
        Response(
            [
                'data: {"choices":[{"delta":{"content":"<final>工具调用完成</final>"},"finish_reason":"stop"}]}\n'.encode("utf-8"),
                b'data: [DONE]\n',
            ]
        ),
    ]

    def fake_open(request, timeout):
        del timeout
        requests.append(json.loads(request.data.decode("utf-8")))
        return responses.pop(0)

    monkeypatch.setattr("hh_agent_pro.providers.openai_chat.urllib.request.urlopen", fake_open)
    client = OpenAIChatClient(
        "deepseek-v4-pro", "https://api.deepseek.com", "key", 0.2, 5
    )
    agent = make_agent(tmp_path, model=client, approval="never")

    assert agent.ask("列出目录后回答") == "工具调用完成"
    assistant_call = next(
        message
        for message in requests[1]["messages"]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    tool_result = next(
        message for message in requests[1]["messages"] if message.get("role") == "tool"
    )
    assert assistant_call["reasoning_content"] == "private-part-1 private-part-2"
    assert assistant_call["tool_calls"][0]["id"] == "call_ds_42"
    assert tool_result["tool_call_id"] == "call_ds_42"

    session_call = next(
        item for item in agent.session["history"] if item.get("tool_call")
    )
    assert session_call["reasoning_content"] == "private-part-1 private-part-2"
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert "private-part-1" not in trace


def test_deepseek_v4_legacy_tool_turn_without_reasoning_degrades_safely(tmp_path):
    client = OpenAIChatClient(
        "deepseek-v4-pro", "https://api.deepseek.com", "key", 0.2, 5
    )
    agent = make_agent(tmp_path, model=client, approval="never")
    agent.session["history"] = [
        {"role": "user", "content": "读取目录"},
        {
            "role": "assistant",
            "content": "",
            "tool_call": {
                "id": "call_old",
                "name": "list_files",
                "args": {"path": "docs"},
            },
        },
        {
            "role": "tool",
            "name": "list_files",
            "tool_call_id": "call_old",
            "content": "[F] docs/guide.md",
        },
    ]

    messages = agent.context_manager.transcript_messages()

    assert not any(message.get("tool_calls") for message in messages)
    assert not any(message.get("role") == "tool" for message in messages)
    assert any(
        message.get("role") == "user"
        and "Tool observation [list_files]" in message.get("content", "")
        for message in messages
    )


def test_deepseek_stream_preserves_length_finish_reason(monkeypatch):
    class Response:
        lines = [
            b'data: {"choices":[{"delta":{"content":"<final>partial"}}]}\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"length"}],"usage":{"prompt_tokens":10,"completion_tokens":100}}\n',
            b"data: [DONE]\n",
        ]

        def __enter__(self):
            return iter(self.lines)

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(
        "hh_agent_pro.providers.openai_chat.urllib.request.urlopen",
        lambda request, timeout: Response(),
    )
    client = OpenAIChatClient(
        "deepseek-chat", "https://api.deepseek.com", "key", 0.2, 5
    )

    assert client.complete(ModelRequest("rules", [{"role": "user", "content": "prompt"}], 100)) == "<final>partial"
    assert client.last_completion_metadata["finish_reason"] == "length"
    assert client.last_completion_metadata["output_truncated"] is True
    assert client.last_completion_metadata["max_output_tokens_requested"] == 100


def test_length_truncation_gets_one_no_tool_continuation_and_merges_answer(tmp_path):
    class TruncatingThenFinishingModel(ScriptedModel):
        def __init__(self):
            super().__init__(["<final>第一部分：工作记忆", "<final>；第二部分：持久记忆。</final>"])
            self.calls = []

        def complete(self, request):

            prompt = request.text()
            self.calls.append(prompt)
            result = super().complete(request)
            self.last_completion_metadata = {
                "finish_reason": "length" if len(self.calls) == 1 else "stop",
                "output_truncated": len(self.calls) == 1,
            }
            return result

    model = TruncatingThenFinishingModel()
    agent = make_agent(tmp_path, model=model, approval="never")

    answer = agent.ask("解释记忆系统")

    assert answer == "第一部分：工作记忆；第二部分：持久记忆。"
    assert agent.current_run_state.status == "completed"
    assert agent.current_run_state.partial_answer == "第一部分：工作记忆"
    assert "Runtime output recovery (authoritative)" in model.calls[-1]
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert '"event": "model_output_truncated"' in trace
    assert '"event": "output_recovery_completed"' in trace


def test_second_length_truncation_stops_and_preserves_partial_draft(tmp_path):
    class AlwaysTruncatedModel(ScriptedModel):
        def __init__(self):
            super().__init__(["<final>第一部分", "<final>第二部分"])

        def complete(self, request):

            result = super().complete(request)
            self.last_completion_metadata = {"finish_reason": "length", "output_truncated": True}
            return result

    agent = make_agent(tmp_path, model=AlwaysTruncatedModel(), approval="never")

    answer = agent.ask("生成很长的说明")

    assert "reached the output-token limit twice" in answer
    assert agent.current_run_state.status == "stopped"
    assert agent.current_run_state.stop_reason == STOP_REASON_OUTPUT_TRUNCATED
    assert agent.current_run_state.partial_answer == "第一部分第二部分"
    report = agent.run_store.load_report(agent.current_run_state.run_id)
    assert report["run_state"]["partial_answer"] == "第一部分第二部分"
    assert agent.session["pending_output"]["partial_answer"] == "第一部分第二部分"


def test_continue_resumes_session_pending_output_instead_of_restarting(tmp_path):
    class CrossRunModel(ScriptedModel):
        def __init__(self):
            super().__init__(["<final>第一部分", "<final>第二部分", "<final>；最终部分。</final>"])
            self.calls = []

        def complete(self, request):

            prompt = request.text()
            self.calls.append(prompt)
            result = super().complete(request)
            truncated = len(self.calls) <= 2
            self.last_completion_metadata = {
                "finish_reason": "length" if truncated else "stop",
                "output_truncated": truncated,
            }
            return result

    model = CrossRunModel()
    agent = make_agent(tmp_path, model=model, approval="never")
    first = agent.ask("生成很长的说明")

    assert "reached the output-token limit twice" in first
    assert agent.session["pending_output"]["partial_answer"] == "第一部分第二部分"

    answer = agent.ask("继续")

    assert answer == "第一部分第二部分；最终部分。"
    assert "Runtime output recovery (authoritative)" in model.calls[-1]
    assert "第一部分第二部分" in model.calls[-1]
    assert agent.session["pending_output"] == {}


def test_dsml_tool_protocol_is_rejected_then_recovered_without_leaking(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    read_a = '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>'
    read_again = '<tool>{"name":"read_file","args":{"path":"a.txt","start":1}}</tool>'
    dsml = (
        '<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="read_file">\n'
        '<｜｜DSML｜｜parameter name="path">a.txt</｜｜DSML｜｜parameter>\n'
        '</｜｜DSML｜｜invoke>\n</｜｜DSML｜｜tool_calls>'
    )
    model = ScriptedModel([read_a, read_again, dsml, "<final>基于已有证据完成回答。</final>"])
    agent = make_agent(tmp_path, model=model, approval="never")
    agent.max_steps = 1

    answer = agent.ask("读取后回答")

    assert answer == "基于已有证据完成回答。"
    assert "DSML" not in answer
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert '"event": "internal_tool_protocol_rejected"' in trace


def test_repeated_dsml_protocol_stops_without_marking_success(tmp_path):
    dsml = '<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name="read_file"></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>'
    agent = make_agent(tmp_path, model=ScriptedModel([dsml, dsml]), approval="never")

    answer = agent.ask("test provider protocol guard")

    assert "protocol was blocked" in answer
    assert "<｜｜DSML｜｜" not in answer
    assert agent.current_run_state.status == "stopped"
    assert agent.current_run_state.stop_reason == STOP_REASON_MODEL_PROTOCOL_ERROR


def test_incomplete_final_tag_is_not_accepted_as_success():
    kind, notice = CodingAgent.parse("<final>unfinished")

    assert kind == "retry"
    assert "incomplete <final>" in notice


@pytest.mark.parametrize(
    "placeholder",
    ["...", "……", " . . . ", "。。。", "<final>...</final>", "<final>……</final>"],
)
def test_placeholder_answer_is_not_accepted_as_success(placeholder):
    kind, notice = CodingAgent.parse(placeholder)

    assert kind == "retry"
    assert "placeholder" in notice


def test_parser_retry_is_runtime_correction_not_assistant_history(tmp_path):
    class TrackingModel(ScriptedModel):
        def __init__(self):
            super().__init__(["<final>unfinished", "<final>完整回答</final>"])
            self.prompts = []

        def complete(self, request):

            prompt = request.text()
            self.prompts.append(prompt)
            return super().complete(request)

    model = TrackingModel()
    agent = make_agent(tmp_path, model=model, approval="never")

    assert agent.ask("解释工具流程") == "完整回答"
    assistant_messages = [
        item["content"] for item in agent.session["history"]
        if item["role"] == "assistant"
    ]
    assert all("Runtime notice" not in content for content in assistant_messages)
    assert "Runtime format correction (authoritative)" in model.prompts[-1]
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert '"event": "model_output_rejected"' in trace


def test_deepseek_request_separates_system_rules_and_role_aware_tool_messages(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return iter([b'data: {"choices":[{"delta":{"content":"<final>done</final>"}}]}\n', b"data: [DONE]\n"])

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_open(request, timeout):
        del timeout
        captured.update(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr("hh_agent_pro.providers.openai_chat.urllib.request.urlopen", fake_open)
    client = OpenAIChatClient("deepseek-chat", "https://api.deepseek.com", "key", 0.2, 5)
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "source"},
    ]

    assert client.complete(ModelRequest("rules", messages, 100, tools=[])) == "<final>done</final>"
    assert captured["messages"][0] == {"role": "system", "content": "rules"}
    assert captured["messages"][1:] == messages


def test_interleaved_large_reads_reuse_evidence_instead_of_reaching_step_limit(tmp_path):
    for name, symbol in (("a.py", "AStore"), ("b.py", "BPolicy"), ("c.py", "WorkingMemory")):
        (tmp_path / name).write_text(
            f'class {symbol}:\n    pass\n\n' + "# padding\n" * 450,
            encoding="utf-8",
        )

    calls = [
        '<tool>{"name":"read_files","args":{"paths":["a.py","b.py"]}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"c.py"}}</tool>',
        '<tool>{"name":"read_files","args":{"paths":["a.py","b.py"]}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"c.py"}}</tool>',
        '<tool>{"name":"read_files","args":{"paths":["a.py","b.py","c.py"]}}</tool>',
        "<final>the modules have separate responsibilities</final>",
    ]
    agent = make_agent(tmp_path, model=ScriptedModel(calls), approval="never")

    answer = agent.ask("compare the memory package and working-memory module")

    assert answer == "the modules have separate responsibilities"
    assert agent.current_run_state.tool_steps == 3
    assert agent.current_run_state.status == "completed"
    report = agent.run_store.load_report(agent.current_run_state.run_id)
    assert set(report["source_evidence"]["files"]) == {"a.py", "b.py", "c.py"}
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert trace.count('"event": "tool_cache_hit"') == 2


def test_provider_events_are_written_to_trace(tmp_path):
    class StreamingModel(ScriptedModel):
        def complete(self, request):
            request.event_callback({"type": "provider_status", "message": "reconnecting 2/5"})
            return "<final>done</final>"

    events = []
    agent = make_agent(tmp_path, model=StreamingModel(), event_sink=CallbackEventSink(events.append))

    assert agent.ask("test") == "done"
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert '"event": "provider_event"' in trace
    assert "reconnecting 2/5" in trace
    assert '"event": "model_decision"' in trace
    assert '"kind": "final"' in trace


def test_keyboard_interrupt_finishes_run_instead_of_leaving_running(tmp_path):
    class InterruptingModel(ScriptedModel):
        def complete(self, request):
            raise KeyboardInterrupt

    agent = make_agent(tmp_path, model=InterruptingModel())

    with pytest.raises(KeyboardInterrupt):
        agent.ask("long task")

    assert agent.current_run_state.stop_reason == STOP_REASON_INTERRUPTED
    persisted = agent.run_store.load_run_state(agent.current_run_state.run_id)
    assert persisted["status"] == "stopped"
    assert persisted["finished_at"]
