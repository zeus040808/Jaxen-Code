import sys
import time
import json
from pathlib import Path

import pytest

from hh_agent_pro.checkpoint import MAX_CHECKPOINTS
from hh_agent_pro.features.cron_scheduler import cron_matches, validate_cron
from hh_agent_pro.run_state import RunState, STATUS_FAILED, STATUS_STOPPED, STOP_REASON_MODEL_ERROR, STOP_REASON_STEP_LIMIT_REACHED
from hh_agent_pro.runtime import HH, SessionStore
from hh_agent_pro.workspace import WorkspaceContext


class ScriptedModel:
    supports_prompt_cache = False

    def __init__(self, outputs=None):
        self.outputs = list(outputs or ["<final>ok</final>"])
        self.last_completion_metadata = {"model": "scripted"}

    def complete(self, prompt, max_new_tokens, **kwargs):
        return self.outputs.pop(0) if self.outputs else "<final>ok</final>"


class FailingModel:
    supports_prompt_cache = False
    model = "failing-model"
    last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        raise RuntimeError("backend unavailable")


def make_agent(tmp_path, outputs=None, approval="auto", model=None, max_steps=4):
    workspace = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(str(tmp_path / ".hh" / "sessions"))
    return HH(
        model_client=model or ScriptedModel(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval,
        max_steps=max_steps,
        max_new_tokens=256,
    )


def test_hh_base_runtime_is_preserved_with_run_artifacts(tmp_path):
    (tmp_path / "README.md").write_text("hello hh", encoding="utf-8")
    agent = make_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":5}}</tool>',
            "<final>读完了。</final>",
        ],
    )

    assert "读完" in agent.ask("read README")
    run_dir = agent.current_run_dir
    assert (run_dir / "run_state.json").exists()
    assert (run_dir / "trace.jsonl").exists()
    assert (run_dir / "report.json").exists()


def test_project_task_tools_use_hh_tool_executor(tmp_path):
    agent = make_agent(tmp_path)

    created = agent.run_tool("create_task", {"subject": "Add parser tests"})
    assert "created task_" in created
    task_id = created.split()[1].rstrip(":")
    assert "pending" in agent.run_tool("list_tasks", {})
    assert "claimed" in agent.run_tool("claim_task", {"task_id": task_id, "owner": "tester"})
    assert "completed" in agent.run_tool("complete_task", {"task_id": task_id})


def test_background_shell_extension_collects_notification(tmp_path):
    agent = make_agent(tmp_path)
    command = f'"{sys.executable}" -c "print(\'done\')"'

    result = agent.run_tool("run_shell", {"command": command, "timeout": 20, "background": True})

    assert "background task" in result
    notes = []
    for _ in range(50):
        notes = agent.background_tasks.collect()
        if notes:
            break
        time.sleep(0.02)
    assert notes
    assert "done" in notes[0]


def test_automatic_background_shell_executes_only_once(tmp_path):
    agent = make_agent(tmp_path)
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    command = f'"{sys.executable}" -m py_compile sample.py'

    result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert "background task bg_0001 started" in result
    notes = []
    for _ in range(50):
        notes = agent.background_tasks.collect()
        if notes:
            break
        time.sleep(0.02)
    assert notes
    assert "exit_code: 0" in notes[0]
    assert agent.background_tasks._counter == 1
    assert agent.background_tasks.collect() == []


def test_cron_tools_and_matching(tmp_path):
    agent = make_agent(tmp_path)

    assert validate_cron("*/5 * * * *") is None
    assert validate_cron("*/0 * * * *")
    assert cron_matches("30 9 * * 2", __import__("datetime").datetime(2026, 7, 7, 9, 30))

    result = agent.run_tool("schedule_cron", {"cron": "30 9 * * *", "prompt": "Review tasks"})
    assert "scheduled cron_" in result
    assert "Review tasks" in agent.run_tool("list_crons", {})
    job_id = result.split()[1].rstrip(":")
    assert "cancelled" in agent.run_tool("cancel_cron", {"job_id": job_id})


def test_child_agent_does_not_expose_cron_tools(tmp_path):
    parent = make_agent(tmp_path)
    child = HH(
        model_client=ScriptedModel(),
        workspace=parent.workspace,
        session_store=parent.session_store,
        approval_policy="never",
        depth=1,
        max_depth=1,
        read_only=True,
    )

    assert "schedule_cron" in parent.tools
    assert "schedule_cron" not in child.tools
    assert "create_task" in child.tools


def test_session_store_writes_readable_json_and_rejects_path_escape(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    path = store.save({"id": "session-1", "history": [{"content": "中文"}]})

    assert "中文" in path.read_text(encoding="utf-8")
    assert store.load("session-1")["history"][0]["content"] == "中文"
    assert not list(path.parent.glob("*.tmp"))
    with pytest.raises(ValueError, match="invalid session id"):
        store.load("../outside")


def test_session_store_retries_transient_windows_replace_lock(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    original_replace = Path.replace
    calls = 0

    def flaky_replace(source, target):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(5, "transient file lock")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    path = store.save({"id": "session-retry", "history": []})

    assert calls == 3
    assert store.load("session-retry")["id"] == "session-retry"
    assert not list(path.parent.glob("*.tmp"))


def test_legacy_session_shape_is_repaired_and_reset_clears_recovery_state(tmp_path):
    store = SessionStore(tmp_path / ".hh" / "sessions")
    store.save(
        {
            "id": "legacy",
            "history": "invalid",
            "memory": [],
            "checkpoints": {"current_id": "missing", "items": []},
            "runtime_identity": [],
            "resume_state": [],
        }
    )
    agent = HH.from_session(
        model_client=ScriptedModel(),
        workspace=WorkspaceContext.build(str(tmp_path)),
        session_store=store,
        session_id="legacy",
    )

    assert agent.session["history"] == []
    assert isinstance(agent.session["memory"], dict)
    assert agent.session["checkpoints"] == {"current_id": "", "items": {}}
    assert isinstance(agent.session["runtime_identity"], dict)
    assert agent.resume_state["status"] == "no-checkpoint"

    agent.ask("create a checkpoint")
    assert agent.session["checkpoints"]["current_id"]
    agent.reset()
    assert agent.session["history"] == []
    assert agent.session["checkpoints"] == {"current_id": "", "items": {}}
    assert agent.resume_state["status"] == "no-checkpoint"


def test_step_limit_persists_the_same_checkpoint_in_state_and_report(tmp_path):
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    tool_call = '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":5}}</tool>'
    agent = make_agent(tmp_path, outputs=[tool_call, tool_call, tool_call], max_steps=2)

    answer = agent.ask("keep reading until stopped")
    run_state = agent.run_store.load_run_state(agent.current_run_state.run_id)
    report = agent.run_store.load_report(agent.current_run_state.run_id)

    assert "step limit" in answer
    assert run_state["status"] == STATUS_STOPPED
    assert run_state["stop_reason"] == STOP_REASON_STEP_LIMIT_REACHED
    assert run_state["checkpoint_id"] == report["checkpoint_id"]
    assert run_state["checkpoint_id"] == agent.session["checkpoints"]["current_id"]
    assert run_state["finished_at"]


def test_agent_can_return_final_after_using_the_last_tool_step(tmp_path):
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    tool_call = '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":5}}</tool>'
    agent = make_agent(tmp_path, outputs=[tool_call, "<final>done</final>"], max_steps=1)

    assert agent.ask("read then finish") == "done"
    assert agent.current_run_state.tool_steps == 1
    assert agent.current_run_state.status == "completed"


def test_checkpoint_and_section_reduction_remain_visible_in_prompt(tmp_path):
    agent = make_agent(tmp_path)
    run_state = RunState.create(request_id="req-checkpoint", user_request="resume goal")
    agent.create_checkpoint(run_state, "resume goal", trigger="test")
    for index in range(10):
        agent.record({"role": "user", "content": f"history-{index}-" + "x" * 100})
    agent.context_manager.section_budgets["history"] = 80

    prompt, metadata = agent._build_prompt_and_metadata("continue")

    assert "Task checkpoint:" in prompt
    assert "Current goal: resume goal" in prompt
    assert any(item.get("reason") == "section_budget" for item in metadata["budget_reductions"])


def test_model_failure_finishes_run_and_persists_trace_and_report(tmp_path):
    agent = make_agent(tmp_path, model=FailingModel())

    with pytest.raises(RuntimeError, match="backend unavailable"):
        agent.ask("trigger model failure")

    run_state = agent.run_store.load_run_state(agent.current_run_state.run_id)
    report = agent.run_store.load_report(agent.current_run_state.run_id)
    trace = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8").splitlines()
    ]
    events = [item["event"] for item in trace]

    assert run_state["status"] == STATUS_FAILED
    assert run_state["stop_reason"] == STOP_REASON_MODEL_ERROR
    assert run_state["checkpoint_id"] == report["checkpoint_id"]
    assert run_state["finished_at"]
    assert "model_failed" in events
    assert events[-1] == "run_finished"
    checkpoint = agent.current_checkpoint()
    assert checkpoint["completed"] == []
    assert checkpoint["current_blocker"] == STOP_REASON_MODEL_ERROR


def test_checkpoint_history_is_bounded_and_keeps_a_valid_parent_chain(tmp_path):
    agent = make_agent(tmp_path)
    run_state = RunState.create(request_id="req-test", user_request="bounded checkpoints")

    for index in range(MAX_CHECKPOINTS + 3):
        agent.create_checkpoint(run_state, f"step {index}", trigger="test")

    items = agent.session["checkpoints"]["items"]
    oldest = next(iter(items.values()))
    assert len(items) == MAX_CHECKPOINTS
    assert oldest["parent_checkpoint_id"] == ""
    assert agent.session["checkpoints"]["current_id"] == run_state.checkpoint_id
