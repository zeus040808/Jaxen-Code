"""上下文工程：稳定 system + 只追加的会话记录 + 末尾提醒；按 token 预算，接近上限才压缩。"""

import json
from pathlib import Path

from hh_agent_pro.agent import CodingAgent
from hh_agent_pro.context_manager import CLEARED_TOOL_RESULT, KEEP_RECENT_TOOL_RESULTS, SUMMARY_PREFIX, estimate_tokens
from hh_agent_pro.providers import FakeModelClient
from hh_agent_pro.run_store import RunStore
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.workspace import WorkspaceContext


def _build_agent(tmp_path, outputs=("<final>done</final>",), **kwargs):
    root = Path(tmp_path) / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text("A = 1\n", encoding="utf-8")
    workspace = WorkspaceContext.build(root, repo_root_override=root)
    return CodingAgent(
        model_client=FakeModelClient(list(outputs)),
        workspace=workspace,
        session_store=SessionStore(root / ".hh" / "sessions"),
        run_store=RunStore(root / ".hh" / "runs"),
        approval_policy="auto",
        **kwargs,
    )


def _tool_exchange(agent, index, content):
    call_id = f"call_{index}"
    agent.record({"role": "assistant", "content": "", "tool_call": {"id": call_id, "name": "read_file", "args": {"path": f"f{index}.py"}}})
    agent.record({"role": "tool", "name": "read_file", "args": {"path": f"f{index}.py"}, "content": content, "tool_call_id": call_id})


def _set_transcript_budget(agent, tokens):
    manager = agent.context_manager
    manager.working_budget_tokens = estimate_tokens(manager.system_prompt()) + tokens


def _tool_pairs_intact(messages):
    call_ids = {call["id"] for message in messages for call in message.get("tool_calls", [])}
    tool_ids = {message["tool_call_id"] for message in messages if message["role"] == "tool"}
    return call_ids == tool_ids


# ---- system ---------------------------------------------------------------

def test_instruction_docs_are_injected_in_full_and_readme_is_not(tmp_path):
    root = Path(tmp_path) / "repo"
    root.mkdir(parents=True)
    (root / "AGENTS.md").write_text("# Rules\n" + "keep this rule. " * 400 + "\nFINAL_RULE_MARKER\n", encoding="utf-8")
    (root / "README.md").write_text("README_MARKER\n", encoding="utf-8")
    agent = _build_agent(tmp_path)

    system = agent.context_manager.system_prompt()

    # 旧实现每份文档只留 1200 字符，结尾的规则会被截掉。
    assert "FINAL_RULE_MARKER" in system
    assert "README_MARKER" not in system


def test_system_prompt_stays_identical_after_the_workspace_changes(tmp_path):
    agent = _build_agent(tmp_path)
    first, _ = agent.build_model_request("fix it")

    (agent.root / "a.py").write_text("A = 2\n", encoding="utf-8")
    (agent.root / "new.py").write_text("B = 1\n", encoding="utf-8")
    second, _ = agent.build_model_request("fix it")

    # system 是会话开始时的快照：改文件不能让可缓存的前缀失效。
    assert first.system_prompt == second.system_prompt


# ---- messages -------------------------------------------------------------

def test_transcript_is_append_only_and_reminder_is_last(tmp_path):
    agent = _build_agent(tmp_path)
    agent.record({"role": "user", "content": "fix it"})
    _tool_exchange(agent, 0, "ok")
    first, _ = agent.context_manager.prepare("fix it")

    _tool_exchange(agent, 1, "ok")
    second, _ = agent.context_manager.prepare("fix it")

    assert first.messages[-1]["content"].startswith("<system-reminder>")
    assert second.messages[-1]["content"].startswith("<system-reminder>")
    # 上一轮去掉末尾提醒后，正好是这一轮的前缀，缓存才能命中。
    assert second.messages[: len(first.messages) - 1] == first.messages[:-1]


def test_reminder_merges_into_trailing_user_message(tmp_path):
    agent = _build_agent(tmp_path)
    agent.record({"role": "user", "content": "fix it"})

    request, _ = agent.context_manager.prepare("fix it")

    assert len(request.messages) == 1
    assert request.messages[0]["content"].startswith("fix it\n\n<system-reminder>")


def test_budget_is_derived_from_the_model_window(tmp_path):
    agent = _build_agent(tmp_path, max_new_tokens=8192)
    agent.model_client.context_window = 1_048_576
    assert agent.context_manager.budget_tokens() == 128_000  # 工作预算更小

    agent.model_client.context_window = 32_000
    # 窗口 − 输出预留 − 余量（至少 4096）
    assert agent.context_manager.budget_tokens() == 32_000 - 8192 - 4096


def test_no_compaction_when_well_under_budget(tmp_path):
    agent = _build_agent(tmp_path)
    agent.record({"role": "user", "content": "fix it"})
    _tool_exchange(agent, 0, "x" * 2000)

    _, metadata = agent.context_manager.prepare("fix it")

    assert metadata["compaction"] is None


# ---- 压缩 -------------------------------------------------------------------

def test_light_compaction_clears_only_old_bulky_tool_results(tmp_path):
    agent = _build_agent(tmp_path)
    agent.record({"role": "user", "content": "fix it"})
    for index in range(KEEP_RECENT_TOOL_RESULTS + 4):
        _tool_exchange(agent, index, f"RESULT_{index} " + "x" * 2000)
    total = agent.context_manager.estimate(agent.context_manager.system_prompt(), agent.context_manager.transcript_messages())
    agent.context_manager.working_budget_tokens = int(total / 0.7)  # 落在 60%~80% 之间

    request, metadata = agent.context_manager.prepare("fix it")

    assert metadata["compaction"]["kind"] == "light"
    text = request.text()
    assert "RESULT_0 " not in text and CLEARED_TOOL_RESULT.split("(")[0] in text
    assert f"RESULT_{KEEP_RECENT_TOOL_RESULTS + 3} " in text  # 最近的保留原文
    assert _tool_pairs_intact(request.messages)
    # 历史本身不删，只是渲染时替换。
    assert "RESULT_0 " in json.dumps(agent.session["history"])


def test_summary_compaction_replaces_old_turns_with_a_structured_summary(tmp_path):
    agent = _build_agent(tmp_path, outputs=["1. User request and goals\nfix parser\n5. Current state and next steps\nrun tests"])
    agent.record({"role": "user", "content": "ORIGINAL_REQUEST fix the parser"})
    for index in range(12):
        _tool_exchange(agent, index, f"RESULT_{index} " + "y" * 3000)
    _set_transcript_budget(agent, 2000)

    request, metadata = agent.context_manager.prepare("ORIGINAL_REQUEST fix the parser")

    assert metadata["compaction"]["kind"] == "summary"
    assert request.messages[0]["content"].startswith(SUMMARY_PREFIX)
    assert "fix parser" in request.messages[0]["content"]
    assert "RESULT_0 " not in request.text()
    # 原始请求被摘要掉后，原文放回末尾提醒。
    assert "Current user request:\nORIGINAL_REQUEST" in request.messages[-1]["content"]
    assert _tool_pairs_intact(request.messages)
    # 摘要请求本身不带工具，用的是专门的摘要 system prompt。
    summary_call = agent.model_client.requests[-1]
    assert summary_call.tools is None and "compact the conversation" in summary_call.system_prompt


def test_summary_failure_falls_back_without_failing_the_task(tmp_path):
    agent = _build_agent(tmp_path, outputs=[])  # 摘要调用会抛 "ran out of outputs"
    agent.record({"role": "user", "content": "fix it"})
    for index in range(KEEP_RECENT_TOOL_RESULTS + 6):
        _tool_exchange(agent, index, "z" * 3000)
    _set_transcript_budget(agent, 2000)

    request, metadata = agent.context_manager.prepare("fix it")

    assert metadata["compaction"]["kind"].startswith("light")
    assert agent.context_manager.estimate(request.system_prompt, request.messages) <= agent.context_manager.budget_tokens()
    assert _tool_pairs_intact(request.messages)


def test_trim_fallback_keeps_newest_turns_and_tool_pairs(tmp_path):
    agent = _build_agent(tmp_path)
    agent.feature_flags = {**agent.feature_flags, "context_summary": False}
    agent.record({"role": "user", "content": "fix it"})
    for index in range(20):
        agent.record({"role": "assistant", "content": f"NOTE_{index} " + "n" * 400})
    _tool_exchange(agent, 99, "short")
    _set_transcript_budget(agent, 300)

    request, metadata = agent.context_manager.prepare("fix it")

    assert "trim" in metadata["compaction"]["kind"]
    assert "NOTE_0 " not in request.text()
    assert "call_99" in json.dumps(request.messages)
    assert _tool_pairs_intact(request.messages)


def test_usage_calibrates_the_token_estimate(tmp_path):
    agent = _build_agent(tmp_path)
    agent.record({"role": "user", "content": "fix it"})
    manager = agent.context_manager
    _, metadata = manager.prepare("fix it")

    manager.observe_usage({"input_tokens": metadata["estimated_input_tokens"] * 2})

    assert manager.calibration > 1.0


def test_compaction_during_a_run_leaves_an_auditable_checkpoint(tmp_path):
    agent = _build_agent(tmp_path, outputs=["<final>done</final>"])
    agent.feature_flags = {**agent.feature_flags, "context_summary": False}
    for index in range(20):
        agent.record({"role": "assistant", "content": f"NOTE_{index} " + "n" * 400})
    _set_transcript_budget(agent, 300)

    assert agent.ask("fix it") == "done"

    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert '"trigger": "context_reduction"' in trace


def test_trim_fallback_keeps_a_long_user_request_in_full(tmp_path):
    # 摘要失败后兜底裁剪会先丢最旧的消息，也就是用户请求；提醒里的任务目标只留前 1000 字符。
    # 2026-11-05 压缩评测发现：超过 1000 字符的请求（SWE-bench 的 issue 常见）后半段会消失。
    agent = _build_agent(tmp_path, outputs=())  # 没有可用输出：摘要请求失败
    request = "Fix the parser. " + "Details about the bug. " * 120 + "END-OF-REQUEST-MARKER"
    agent.task_state.begin(request, "run_test")
    agent.record({"role": "user", "content": request})
    for index in range(30):
        _tool_exchange(agent, index, "x" * 3000)
    _set_transcript_budget(agent, 6000)

    request_obj, metadata = agent.context_manager.prepare(request)

    assert "trim" in metadata["compaction"]["kind"]
    assert any(request in str(message.get("content") or "") for message in request_obj.messages)
    assert metadata["estimated_input_tokens"] <= metadata["budget_tokens"]
    assert _tool_pairs_intact(request_obj.messages)


class _SummaryModel:
    """摘要请求返回固定文本，并可设置 finish_reason；记录收到的请求。"""

    model = "fake"
    context_window = 128_000
    max_output_tokens = 16_384

    def __init__(self, text, finish_reason="stop"):
        self.text, self.finish_reason, self.requests = text, finish_reason, []
        self.last_completion_metadata = {}

    def complete(self, request):
        self.requests.append(request)
        self.last_completion_metadata = {"finish_reason": self.finish_reason}
        return self.text


def _over_summary_threshold(tmp_path, model):
    import types
    agent = _build_agent(tmp_path)
    agent.model_client = model
    agent.current_run_state = types.SimpleNamespace(run_id="run_1", attempts=1, tool_steps=0)
    agent.record({"role": "user", "content": "fix it"})
    for index in range(12):
        _tool_exchange(agent, index, f"RESULT_{index} " + "y" * 3000)
    _set_transcript_budget(agent, 2000)
    return agent


# 2026-11-05 真实模型评测：28 次摘要里 29% 把对话接着往下写、7% 被截断，原来都会被当成摘要接受。
def test_summary_that_continues_the_conversation_is_rejected(tmp_path):
    model = _SummaryModel('[assistant tool call] read_file({"path": "a.py"})\n\n[tool result: read_file]\n 1: INVENTED_CONTENT')
    agent = _over_summary_threshold(tmp_path, model)

    request, metadata = agent.context_manager.prepare("fix it")

    assert metadata["compaction"]["summary_rejected"] == "continued_conversation"
    assert agent.session["context"]["summary"] == ""
    assert "INVENTED_CONTENT" not in request.text()


def test_truncated_summary_is_rejected(tmp_path):
    model = _SummaryModel("1. User request and goals\nfix it\n2. Key decisions", finish_reason="length")
    agent = _over_summary_threshold(tmp_path, model)

    _, metadata = agent.context_manager.prepare("fix it")

    assert metadata["compaction"]["summary_rejected"] == "truncated"
    assert agent.session["context"]["summary"] == ""


def test_summary_is_not_retried_in_the_same_run_after_a_failure(tmp_path):
    model = _SummaryModel("")
    agent = _over_summary_threshold(tmp_path, model)

    agent.context_manager.prepare("fix it")
    _tool_exchange(agent, 99, "w" * 3000)
    agent.context_manager.prepare("fix it")

    assert len(model.requests) == 1


def test_summary_request_wraps_the_transcript_and_ends_with_the_instruction(tmp_path):
    model = _SummaryModel("1. User request and goals\nfix it\n5. Current state and next steps\ndone")
    agent = _over_summary_threshold(tmp_path, model)

    _, metadata = agent.context_manager.prepare("fix it")

    content = model.requests[0].messages[0]["content"]
    assert "<conversation>" in content and "</conversation>" in content
    assert content.rstrip().endswith("using exactly the five numbered sections.")
    assert model.requests[0].max_new_tokens >= 16_384
    assert metadata["compaction"]["kind"] == "summary"

