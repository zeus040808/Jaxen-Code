import io
import json
import subprocess
from types import SimpleNamespace

from hh_agent_pro.cli import _build_model_client, _effective_model, _switch_model, build_arg_parser
from hh_agent_pro.providers.clients import OpenAIChatCompatibleModelClient
from hh_agent_pro.providers.codex_cli import CodexCliModelClient
from hh_agent_pro.streaming import CallbackEventSink
from hh_agent_pro.terminal_ui import TerminalEventSink
from hh_agent_pro.runtime import HH
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.workspace import WorkspaceContext


class FakeProcess:
    def __init__(self, stdout_lines, stderr_lines=(), return_code=0):
        self.stdin = RecordingStdin()
        self.stdout = io.StringIO("".join(stdout_lines))
        self.stderr = io.StringIO("".join(stderr_lines))
        self.return_code = return_code
        self.killed = False

    def wait(self, timeout=None):
        del timeout
        return self.return_code

    def kill(self):
        self.killed = True


class RecordingStdin(io.StringIO):
    def __init__(self):
        super().__init__()
        self.was_closed = False

    def close(self):
        self.was_closed = True


class FakeHttpResponse:
    def __init__(self, lines):
        self.lines = [line.encode("utf-8") for line in lines]

    def __enter__(self):
        return iter(self.lines)

    def __exit__(self, exc_type, exc, traceback):
        return False



def json_line(value):
    return json.dumps(value, ensure_ascii=False) + "\n"


def make_client(tmp_path, monkeypatch, events, stderr=(), return_code=0):
    monkeypatch.setattr("hh_agent_pro.providers.codex_cli.shutil.which", lambda executable: "C:/fake/codex.exe")
    process = FakeProcess([json_line(event) for event in events], stderr, return_code)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    return CodexCliModelClient(tmp_path, timeout=5), process


def test_deepseek_v4_pro_is_default_and_codex_remains_available():
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.provider == "deepseek"
    assert _effective_model(args, "deepseek") == "deepseek-v4-pro"

    provider_action = next(action for action in parser._actions if action.dest == "provider")
    assert "codex" in provider_action.choices
    assert "deepseek" in provider_action.choices
    assert args.max_steps == 12
    assert args.max_new_tokens == 2048


def test_deepseek_uses_its_own_environment_configuration(monkeypatch):
    monkeypatch.setenv("HH_DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("HH_DEEPSEEK_MODEL", "deepseek-reasoner")
    monkeypatch.setenv("HH_DEEPSEEK_API_BASE", "https://api.deepseek.example")
    args = build_arg_parser().parse_args(["--provider", "deepseek"])

    client = _build_model_client(args)

    assert isinstance(client, OpenAIChatCompatibleModelClient)
    assert client.provider == "deepseek"
    assert client.model == "deepseek-reasoner"
    assert client.base_url == "https://api.deepseek.example/v1"


def test_deepseek_missing_key_fails_before_replacing_current_model(monkeypatch):
    monkeypatch.delenv("HH_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    args = build_arg_parser().parse_args([])
    agent = SimpleNamespace(
        model_client=SimpleNamespace(provider="codex", model="codex-config-default"),
        workspace=SimpleNamespace(repo_root="."),
    )
    old_client = agent.model_client

    try:
        _switch_model(agent, args, "deepseek")
    except RuntimeError as exc:
        assert "DeepSeek API key" in str(exc)
    else:
        raise AssertionError("expected missing-key error")
    assert agent.model_client is old_client


def test_runtime_model_switch_preserves_agent_and_uses_requested_model(monkeypatch):
    args = build_arg_parser().parse_args([])
    agent = SimpleNamespace(
        model_client=SimpleNamespace(provider="codex", model="codex-config-default"),
        workspace=SimpleNamespace(repo_root="C:/workspace"),
        session={"id": "same-session"},
    )
    replacement = SimpleNamespace(provider="deepseek", model="deepseek-reasoner")
    captured = {}

    def fake_build(switch_args):
        captured.update(vars(switch_args))
        return replacement

    monkeypatch.setattr("hh_agent_pro.cli._build_model_client", fake_build)
    message = _switch_model(agent, args, "deepseek", "deepseek-reasoner")

    assert agent.model_client is replacement
    assert agent.session["id"] == "same-session"
    assert captured["cwd"] == "C:/workspace"
    assert captured["model"] == "deepseek-reasoner"
    assert "codex / codex-config-default -> deepseek / deepseek-reasoner" in message


def test_deepseek_stream_consumes_sse_and_emits_safe_status(monkeypatch):
    chunks = [
        'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}\n',
        'data: {"choices":[{"delta":{"content":"<final>"}}]}\n',
        'data: {"choices":[{"delta":{"content":"done</final>"}}]}\n',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n',
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(
        "hh_agent_pro.providers.clients.urllib.request.urlopen",
        lambda request, timeout: FakeHttpResponse(chunks),
    )
    client = OpenAIChatCompatibleModelClient(
        model="deepseek-reasoner",
        base_url="https://api.deepseek.com",
        api_key="test-key",
        temperature=0.2,
        timeout=5,
    )
    events = []

    result = client.complete_stream("prompt", 64, event_callback=events.append)

    assert result == "<final>done</final>"
    messages = [event["message"] for event in events]
    assert messages == [
        "DeepSeek turn started",
        "DeepSeek reasoning in progress",
        "DeepSeek turn completed",
    ]
    assert all("private reasoning" not in message for message in messages)
    assert client.last_completion_metadata["input_tokens"] == 5


def test_build_codex_client_uses_local_config_model_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr("hh_agent_pro.providers.codex_cli.shutil.which", lambda executable: "C:/fake/codex.exe")
    args = SimpleNamespace(
        provider="codex",
        cwd=str(tmp_path),
        codex_path="codex",
        codex_timeout=17,
        model=None,
    )
    client = _build_model_client(args)
    assert isinstance(client, CodexCliModelClient)
    assert client.model == "codex-config-default"
    assert "--model" not in client._command()


def test_codex_jsonl_stream_returns_agent_message_and_emits_supported_summary(tmp_path, monkeypatch):
    events = [
        {"type": "thread.started", "thread_id": "thread-123"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "r1", "type": "reasoning", "text": "先检查配置，再决定。"}},
        {"type": "error", "message": "Reconnecting... 2/5"},
        {"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": "<final>完成</final>"}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "output_tokens": 7,
                "reasoning_output_tokens": 3,
            },
        },
    ]
    client, process = make_client(tmp_path, monkeypatch, events)
    streamed = []

    result = client.complete_stream(
        "prompt",
        128,
        event_callback=streamed.append,
    )

    assert result == "<final>完成</final>"
    assert any(event["type"] == "reasoning_summary" for event in streamed)
    assert any(event["type"] == "provider_status" and "Reconnecting" in event["message"] for event in streamed)
    assert client.last_completion_metadata["codex_thread_id"] == "thread-123"
    assert client.last_completion_metadata["cached_tokens"] == 4
    assert process.stdin.was_closed


def test_codex_command_is_ephemeral_read_only_and_prompt_forbids_inner_tools(tmp_path, monkeypatch):
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "<final>ok</final>"}},
        {"type": "turn.completed", "usage": {}},
    ]
    client, process = make_client(tmp_path, monkeypatch, events)
    client.complete_stream("HH PROMPT", 64)

    command = client._command()
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    prompt = process.stdin.getvalue()
    assert "Do not use Codex CLI tools" in prompt
    assert prompt.endswith("HH PROMPT")


def test_codex_missing_answer_surfaces_errors(tmp_path, monkeypatch):
    events = [
        {"type": "error", "message": "authentication required"},
        {"type": "turn.completed", "usage": {}},
    ]
    client, _ = make_client(tmp_path, monkeypatch, events, return_code=1)
    try:
        client.complete_stream("prompt", 64)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")
    assert "codex login status" in message
    assert "authentication required" in message


def test_terminal_stream_renders_status_tools_reasoning_and_user_prompt():
    stream = io.StringIO()
    sink = TerminalEventSink(stream=stream, use_color=False)
    sink.emit("run_started", request="test")
    sink.emit("status", message="Thinking · step 1/6")
    sink.emit("reasoning_summary", text="Inspect the configuration.")
    sink.emit("tool_started", name="read_file", args={"path": "README.md"})
    sink.emit("tool_finished", name="read_file", result="hello", metadata={"tool_status": "ok"})
    sink.emit("needs_user_input", question="请选择：\n1. A\n2. B")
    sink.emit("run_finished", status="waiting_for_user", final_answer="请选择：\n1. A\n2. B")

    output = stream.getvalue()
    assert "Thinking" in output
    assert "Reasoning summary" in output
    assert "read_file" in output and "hello" in output
    assert "Need your input" in output
    assert sink.needs_input is True


def test_callback_event_sink_preserves_structured_event():
    events = []
    sink = CallbackEventSink(events.append)
    sink.emit("tool_started", name="search", args={"pattern": "Codex"})
    assert events == [{"type": "tool_started", "name": "search", "args": {"pattern": "Codex"}}]


def test_terminal_sink_replaces_unencodable_symbols_in_legacy_console():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    sink = TerminalEventSink(stream=stream, use_color=False)
    sink.emit("provider_status", message="中文状态")
    stream.flush()
    assert b"->" in raw.getvalue()


class StreamingScriptedModel:
    supports_prompt_cache = False

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.last_completion_metadata = {}

    def complete_stream(self, prompt, max_new_tokens, event_callback=None, **kwargs):
        del prompt, max_new_tokens, kwargs
        if event_callback:
            event_callback({"type": "provider_status", "message": "Codex turn started"})
            event_callback({"type": "reasoning_summary", "text": "先读取文件，再回答。"})
        return self.outputs.pop(0)


def test_agent_loop_streams_reasoning_tool_and_final_answer(tmp_path):
    (tmp_path / "README.md").write_text("hello codex\n", encoding="utf-8")
    events = []
    agent = HH(
        model_client=StreamingScriptedModel(
            [
                '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":5}}</tool>',
                "<final>README contains hello codex.</final>",
            ]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".hh" / "sessions"),
        approval_policy="auto",
        event_sink=CallbackEventSink(events.append),
    )

    assert agent.ask("read README") == "README contains hello codex."
    event_types = [event["type"] for event in events]
    assert "reasoning_summary" in event_types
    assert "tool_started" in event_types
    assert "tool_finished" in event_types
    assert event_types[-1] == "run_finished"


def test_agent_loop_streams_needs_user_input(tmp_path):
    events = []
    agent = HH(
        model_client=StreamingScriptedModel(
            ["<needs_user_input>请选择：\n1. 修改现有文件\n2. 创建新文件</needs_user_input>"]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".hh" / "sessions"),
        approval_policy="auto",
        event_sink=CallbackEventSink(events.append),
    )

    answer = agent.ask("处理文件")
    assert "请选择" in answer
    needs_event = next(event for event in events if event["type"] == "needs_user_input")
    assert "1. 修改现有文件" in needs_event["question"]
