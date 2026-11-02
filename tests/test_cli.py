import io
from types import SimpleNamespace

import pytest

from hh_agent_pro.agent import CodingAgent
from hh_agent_pro.cli import PROVIDER_NAMES, _build_model_client, _effective_model, _switch_model, build_arg_parser
from hh_agent_pro.providers import ModelRequest, OpenAIChatClient
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.streaming import CallbackEventSink
from hh_agent_pro.terminal_ui import TerminalEventSink
from hh_agent_pro.workspace import WorkspaceContext


class FakeHttpResponse:
    def __init__(self, lines):
        self.lines = [line.encode("utf-8") for line in lines]

    def __enter__(self):
        return iter(self.lines)

    def __exit__(self, exc_type, exc, traceback):
        return False


def _clear_provider_env(monkeypatch):
    # 别的测试经 build_agent() 把本机 .env 载入了 os.environ，这里先隔离。
    for name in (
        "HH_DEEPSEEK_MODEL", "DEEPSEEK_MODEL", "HH_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY",
        "HH_DEEPSEEK_API_BASE", "DEEPSEEK_API_BASE",
        "HH_OPENAI_MODEL", "OPENAI_MODEL", "HH_OPENAI_API_KEY", "OPENAI_API_KEY",
        "HH_OPENAI_API_BASE", "OPENAI_API_BASE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_only_openai_compatible_providers_and_flash_default(monkeypatch):
    _clear_provider_env(monkeypatch)
    args = build_arg_parser().parse_args([])

    assert PROVIDER_NAMES == ("deepseek", "openai", "fake")
    assert args.provider == "deepseek"
    assert _effective_model(args, "deepseek") == "deepseek-flash"
    assert args.max_steps == 50
    assert args.max_new_tokens == 8192


def test_deepseek_uses_its_own_environment_configuration(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HH_DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("HH_DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("HH_DEEPSEEK_API_BASE", "https://api.deepseek.example")

    client = _build_model_client(build_arg_parser().parse_args(["--provider", "deepseek"]))

    assert isinstance(client, OpenAIChatClient)
    assert client.provider == "deepseek"
    assert client.model == "deepseek-v4-pro"
    assert client.base_url == "https://api.deepseek.example/v1"
    assert client.context_window == 1_048_576


def test_openai_provider_targets_any_compatible_endpoint(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HH_OPENAI_API_KEY", "test-key")
    args = build_arg_parser().parse_args([
        "--provider", "openai", "--model", "qwen-plus",
        "--base-url", "https://dashscope.example/compatible-mode", "--context-window", "131072",
    ])

    client = _build_model_client(args)

    assert client.model == "qwen-plus"
    assert client.base_url == "https://dashscope.example/compatible-mode/v1"
    assert client.context_window == 131072


def test_openai_provider_requires_model_and_base_url(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HH_OPENAI_API_KEY", "test-key")

    with pytest.raises(RuntimeError, match="base-url|HH_OPENAI_API_BASE"):
        _build_model_client(build_arg_parser().parse_args(["--provider", "openai", "--model", "m"]))


def test_missing_key_fails_before_replacing_current_model(monkeypatch):
    _clear_provider_env(monkeypatch)
    args = build_arg_parser().parse_args([])
    agent = SimpleNamespace(
        model_client=SimpleNamespace(provider="openai", model="qwen-plus"),
        workspace=SimpleNamespace(repo_root="."),
    )
    old_client = agent.model_client

    with pytest.raises(RuntimeError, match="API key is missing"):
        _switch_model(agent, args, "deepseek")
    assert agent.model_client is old_client


def test_runtime_model_switch_preserves_agent_and_uses_requested_model(monkeypatch):
    args = build_arg_parser().parse_args([])
    agent = SimpleNamespace(
        model_client=SimpleNamespace(provider="openai", model="qwen-plus"),
        workspace=SimpleNamespace(repo_root="C:/workspace"),
        session={"id": "same-session"},
    )
    replacement = SimpleNamespace(provider="deepseek", model="deepseek-v4-pro")
    captured = {}

    def fake_build(switch_args):
        captured.update(vars(switch_args))
        return replacement

    monkeypatch.setattr("hh_agent_pro.cli._build_model_client", fake_build)
    message = _switch_model(agent, args, "deepseek", "deepseek-v4-pro")

    assert agent.model_client is replacement
    assert agent.session["id"] == "same-session"
    assert captured["cwd"] == "C:/workspace"
    assert captured["model"] == "deepseek-v4-pro"
    assert "openai / qwen-plus -> deepseek / deepseek-v4-pro" in message


def test_stream_consumes_sse_and_emits_safe_status(monkeypatch):
    chunks = [
        'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}\n',
        'data: {"choices":[{"delta":{"content":"<final>"}}]}\n',
        'data: {"choices":[{"delta":{"content":"done</final>"}}]}\n',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7,"prompt_cache_hit_tokens":3}}\n',
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(
        "hh_agent_pro.providers.openai_chat.urllib.request.urlopen",
        lambda request, timeout: FakeHttpResponse(chunks),
    )
    client = OpenAIChatClient("deepseek-flash", "https://api.deepseek.com", "test-key", 0.2, 5, label="DeepSeek")
    events = []

    result = client.complete(ModelRequest("system", [{"role": "user", "content": "hi"}], 64, event_callback=events.append))

    assert result == "<final>done</final>"
    messages = [event["message"] for event in events]
    assert messages == ["DeepSeek turn started", "DeepSeek reasoning in progress", "DeepSeek turn completed"]
    assert all("private reasoning" not in message for message in messages)
    assert client.last_completion_metadata["input_tokens"] == 5
    assert client.last_completion_metadata["cached_tokens"] == 3


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
    sink.emit("tool_started", name="search", args={"pattern": "x"})
    assert events == [{"type": "tool_started", "name": "search", "args": {"pattern": "x"}}]


def test_terminal_sink_replaces_unencodable_symbols_in_legacy_console():
    """老式控制台（ascii/gbk）遇到不可编码字符必须降级，而不是抛异常。"""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    sink = TerminalEventSink(stream=stream, use_color=False)
    sink.emit("provider_status", message="中文状态")
    stream.flush()

    written = raw.getvalue()
    assert written.strip(), "状态行不能被整条吞掉"
    assert b"?" in written, "不可编码字符应被替换而非导致异常"


class StreamingScriptedModel:
    context_window = 128_000

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.last_completion_metadata = {}

    def complete(self, request):
        if request.event_callback:
            request.event_callback({"type": "provider_status", "message": "Model turn started"})
            request.event_callback({"type": "reasoning_summary", "text": "先读取文件，再回答。"})
        return self.outputs.pop(0)


def test_agent_loop_streams_reasoning_tool_and_final_answer(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    events = []
    agent = CodingAgent(
        model_client=StreamingScriptedModel([
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":5}}</tool>',
            "<final>README contains hello.</final>",
        ]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".hh" / "sessions"),
        approval_policy="auto",
        event_sink=CallbackEventSink(events.append),
    )

    assert agent.ask("read README") == "README contains hello."
    event_types = [event["type"] for event in events]
    assert "reasoning_summary" in event_types
    assert "tool_started" in event_types
    assert "tool_finished" in event_types
    assert event_types[-1] == "run_finished"


def test_agent_loop_streams_needs_user_input(tmp_path):
    events = []
    agent = CodingAgent(
        model_client=StreamingScriptedModel(["<needs_user_input>请选择：\n1. 修改现有文件\n2. 创建新文件</needs_user_input>"]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".hh" / "sessions"),
        approval_policy="auto",
        event_sink=CallbackEventSink(events.append),
    )

    answer = agent.ask("处理文件")
    assert "请选择" in answer
    needs_event = next(event for event in events if event["type"] == "needs_user_input")
    assert "1. 修改现有文件" in needs_event["question"]
