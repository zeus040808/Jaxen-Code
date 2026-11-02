import sys
import time
import json
import re
from pathlib import Path

import pytest

from hh_agent_pro.checkpoint import MAX_CHECKPOINTS
from hh_agent_pro.services.cron_scheduler import cron_matches, validate_cron
from hh_agent_pro.run_state import RunState, STATUS_COMPLETED, STATUS_FAILED, STATUS_STOPPED, STATUS_WAITING, STOP_REASON_APPROVAL_DENIED, STOP_REASON_FINAL_ANSWER_RETURNED, STOP_REASON_MODEL_ERROR, STOP_REASON_NEEDS_USER_INPUT, STOP_REASON_REPEATED_FAILED_CALL
from hh_agent_pro.agent import CodingAgent
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.workspace import WorkspaceContext, file_freshness


class ScriptedModel:

    def __init__(self, outputs=None):
        self.outputs = list(outputs or ["<final>ok</final>"])
        self.last_completion_metadata = {"model": "scripted"}

    def complete(self, request):

        return self.outputs.pop(0) if self.outputs else "<final>ok</final>"


class FailingModel:
    model = "failing-model"
    last_completion_metadata = {}

    def complete(self, request):

        raise RuntimeError("backend unavailable")


def make_agent(tmp_path, outputs=None, approval="auto", model=None, max_steps=4):
    workspace = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(str(tmp_path / ".hh" / "sessions"))
    return CodingAgent(
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


def test_workspace_file_freshness_is_shared_and_boundary_safe(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")

    first = file_freshness("sample.py", root)
    source.write_text("value = 2\n", encoding="utf-8")

    assert first
    assert file_freshness("sample.py", root) != first
    assert file_freshness("../outside.py", root) is None
    assert file_freshness("missing.py", root) is None


def test_checkpoint_with_key_files_can_resume_and_ask(tmp_path):
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    first = make_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"sample.py"}}</tool>',
            "<final>first turn complete</final>",
        ],
    )
    assert first.ask("inspect sample.py") == "first turn complete"
    checkpoint = first.current_checkpoint()
    assert checkpoint["key_files"][0]["path"] == "sample.py"
    assert checkpoint["key_files"][0]["freshness"] == file_freshness("sample.py", tmp_path)

    resumed = CodingAgent.from_session(
        model_client=ScriptedModel(["<final>resumed safely</final>"]),
        workspace=WorkspaceContext.build(str(tmp_path)),
        session_store=first.session_store,
        session_id=first.session["id"],
        approval_policy="auto",
        max_steps=4,
        max_new_tokens=256,
    )

    assert resumed.ask("continue") == "resumed safely"
    assert resumed.current_run_state.resume_status == "full-valid"


def test_project_task_tools_use_hh_tool_executor(tmp_path):
    agent = make_agent(tmp_path)

    created = agent.run_tool("create_task", {"subject": "Add parser tests"})
    assert "created task_" in created
    task_id = created.split()[1].rstrip(":")
    assert "pending" in agent.run_tool("list_tasks", {})
    assert "claimed" in agent.run_tool("claim_task", {"task_id": task_id, "owner": "tester"})
    assert "completed" in agent.run_tool("complete_task", {"task_id": task_id})


def test_project_task_acceptance_evidence_and_failure_states(tmp_path):
    agent = make_agent(tmp_path)

    created = agent.run_tool(
        "create_task",
        {
            "subject": "Verify parser",
            "acceptance_criteria": ["parser test passes"],
        },
    )
    task_id = created.split()[1].rstrip(":")
    assert "claimed" in agent.run_tool("claim_task", {"task_id": task_id})
    assert "needs at least 1 evidence" in agent.run_tool("complete_task", {"task_id": task_id})
    assert "recorded verified claim" in agent.run_tool(
        "record_evidence",
        {
            "task_id": task_id,
            "claim": "parser test passes",
            "evidence": "pytest tests/test_parser.py: 4 passed",
            "verified": True,
        },
    )
    assert "completed" in agent.run_tool("complete_task", {"task_id": task_id})
    completed = agent.project_tasks.load(task_id)
    assert completed.status == "completed"
    assert completed.evidence == ["pytest tests/test_parser.py: 4 passed"]

    failed_id = agent.run_tool("create_task", {"subject": "Unavailable dependency"}).split()[1].rstrip(":")
    assert "failed" in agent.run_tool(
        "fail_task",
        {"task_id": failed_id, "reason": "package index unavailable"},
    )
    assert agent.project_tasks.load(failed_id).failure_reason == "package index unavailable"

    blocked_id = agent.run_tool("create_task", {"subject": "Need target path"}).split()[1].rstrip(":")
    assert "blocked" in agent.run_tool(
        "block_task",
        {"task_id": blocked_id, "reason": "target path missing"},
    )
    assert agent.project_tasks.load(blocked_id).status == "blocked"


def test_delivery_gate_rejects_final_until_tracked_subtask_is_verified(tmp_path):
    class PlanningModel(ScriptedModel):
        def __init__(self):
            super().__init__([])
            self.step = 0

        def complete(self, request):

            prompt = request.text()
            self.step += 1
            if self.step == 1:
                return (
                    '<tool>{"name":"create_task","args":{"subject":"Verify change",'
                    '"acceptance_criteria":["verification passes"]}}</tool>'
                )
            task_id = re.search(r"\btask_[A-Za-z0-9_]+\b", prompt).group(0)
            if self.step == 2:
                return "<final>finished too early</final>"
            if self.step == 3:
                return f'<tool>{{"name":"claim_task","args":{{"task_id":"{task_id}"}}}}</tool>'
            if self.step == 4:
                return (
                    f'<tool>{{"name":"complete_task","args":{{"task_id":"{task_id}",'
                    '"evidence":["verification: passed"]}}</tool>'
                )
            return "<final>verified delivery</final>"

    agent = make_agent(tmp_path, model=PlanningModel(), max_steps=5)

    assert agent.ask("make and verify a change") == "verified delivery"
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert '"event": "completion_deferred"' in trace
    assert agent.task_state.to_dict()["status"] == "completed"
    task_id = agent.task_state.to_dict()["subtask_ids"][0]
    assert agent.project_tasks.load(task_id).status == "completed"


def test_unverified_claim_blocks_delivery(tmp_path):
    agent = make_agent(
        tmp_path,
        [
            '<tool>{"name":"record_evidence","args":{"claim":"the fix works",'
            '"evidence":"not tested yet","verified":false}}</tool>',
            "<final>done</final>",
            '<tool>{"name":"record_evidence","args":{"claim":"the fix works",'
            '"evidence":"pytest: 8 passed","verified":true}}</tool>',
            "<final>verified now</final>",
        ],
        max_steps=4,
    )

    assert agent.ask("check the fix") == "verified now"
    assert agent.task_state.to_dict()["claims"][0]["verified"] is True
    assert agent.task_state.to_dict()["claims"][0]["evidence"] == "pytest: 8 passed"
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert "claim is not verified" in trace


def test_task_state_survives_session_resume_and_continues_subtask(tmp_path):
    class ResumeModel(ScriptedModel):
        def __init__(self):
            super().__init__([])
            self.step = 0

        def complete(self, request):

            prompt = request.text()
            self.step += 1
            task_id = re.search(r"\btask_[A-Za-z0-9_]+\b", prompt).group(0)
            if self.step == 1:
                return f'<tool>{{"name":"claim_task","args":{{"task_id":"{task_id}"}}}}</tool>'
            if self.step == 2:
                return (
                    f'<tool>{{"name":"complete_task","args":{{"task_id":"{task_id}",'
                    '"evidence":["user supplied target"]}}</tool>'
                )
            return "<final>resumed and completed</final>"

    first = make_agent(
        tmp_path,
        [
            '<tool>{"name":"create_task","args":{"subject":"Use target",'
            '"acceptance_criteria":["target supplied"]}}</tool>',
            "<needs_user_input>Please provide the target.</needs_user_input>",
        ],
        max_steps=4,
    )
    assert first.ask("configure the target") == "Please provide the target."
    session_id = first.session["id"]
    task_id = first.task_state.to_dict()["subtask_ids"][0]

    resumed = CodingAgent.from_session(
        model_client=ResumeModel(),
        workspace=WorkspaceContext.build(str(tmp_path)),
        session_store=first.session_store,
        session_id=session_id,
        approval_policy="auto",
        max_steps=5,
        max_new_tokens=256,
    )
    assert resumed.ask("The target is production.") == "resumed and completed"
    state = resumed.task_state.to_dict()
    assert state["task_id"] == first.task_state.to_dict()["task_id"]
    assert len(state["run_ids"]) == 2
    assert any(item["kind"] == "user_followup" for item in state["evidence"])
    assert resumed.project_tasks.load(task_id).status == "completed"


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


def test_slow_shell_stays_foreground_without_explicit_background_flag(tmp_path):
    agent = make_agent(tmp_path)
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    command = f'"{sys.executable}" -m py_compile sample.py'

    result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert "exit_code: 0" in result
    assert agent.background_tasks._counter == 0
    assert agent.background_tasks.collect() == []


def test_explicit_background_result_is_collected_inside_same_agent_loop(tmp_path):
    class BackgroundAwareModel(ScriptedModel):
        def __init__(self):
            super().__init__([])
            self.prompts = []

        def complete(self, request):

            prompt = request.text()
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                command = f'"{sys.executable}" -c "print(\'same-run-done\')"'
                return f'<tool>{{"name":"run_shell","args":{{"command":{json.dumps(command)},"timeout":20,"background":true}}}}</tool>'
            if (
                "<task_notification>" in prompt
                and "<status>completed</status>" in prompt
                and "<summary>" in prompt
                and "same-run-done" in prompt
            ):
                return "<final>background result observed</final>"
            time.sleep(0.05)
            return '<tool>{"name":"list_files","args":{"path":"."}}</tool>'

    model = BackgroundAwareModel()
    agent = make_agent(tmp_path, model=model, max_steps=5)

    assert agent.ask("run one explicit background command") == "background result observed"
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert '"event": "background_notification"' in trace


def test_approval_denied_stops_immediately_with_blocked_run_state(tmp_path):
    agent = make_agent(
        tmp_path,
        ['<tool>{"name":"run_shell","args":{"command":"echo denied","timeout":20}}</tool>'],
        approval="never",
    )

    answer = agent.ask("run a command")

    assert "Blocked safely" in answer
    assert agent.current_run_state.status == STATUS_STOPPED
    assert agent.current_run_state.stop_reason == STOP_REASON_APPROVAL_DENIED
    assert agent.current_run_state.attempts == 1
    assert agent.current_run_state.tool_steps == 1


def test_repeated_failed_fingerprint_stops_on_second_same_failure(tmp_path):
    call = '<tool>{"name":"read_file","args":{"path":"missing.py","start":1,"end":20}}</tool>'
    agent = make_agent(tmp_path, [call, call, "<final>should not run</final>"], max_steps=5)

    answer = agent.ask("read the missing file")

    assert "repeated failed tool call" in answer
    assert agent.current_run_state.stop_reason == STOP_REASON_REPEATED_FAILED_CALL
    assert agent.current_run_state.attempts == 2
    assert agent.current_run_state.tool_steps == 2


def test_tool_failure_feedback_is_structured_and_defers_false_final(tmp_path):
    class CapturingModel(ScriptedModel):
        def __init__(self):
            super().__init__(
                [
                    '<tool>{"name":"read_file","args":{"path":"missing.py","start":1,"end":20}}</tool>',
                    "<final>done</final>",
                    "<blocked>The requested file does not exist.</blocked>",
                ]
            )
            self.prompts = []

        def complete(self, request):

            prompt = request.text()
            self.prompts.append(prompt)
            return super().complete(request)

    model = CapturingModel()
    agent = make_agent(tmp_path, model=model, max_steps=5)

    assert agent.ask("read missing.py") == "The requested file does not exist."
    assert '"error_code": "invalid_arguments"' in model.prompts[1]
    assert '"retryable": true' in model.prompts[1]
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert '"event": "completion_deferred"' in trace


def test_needs_user_input_has_explicit_waiting_state(tmp_path):
    agent = make_agent(tmp_path, ["<needs_user_input>Please provide the target timeout.</needs_user_input>"])

    assert agent.ask("change the timeout") == "Please provide the target timeout."
    assert agent.current_run_state.status == STATUS_WAITING
    assert agent.current_run_state.stop_reason == STOP_REASON_NEEDS_USER_INPUT


def test_native_needs_user_input_pseudo_call_is_protocol_state(tmp_path):
    agent = make_agent(
        tmp_path,
        [
            '<tool>{"name":"needs_user_input","args":'
            '{"question":"Which timeout should I use?"}}</tool>'
        ],
    )

    assert agent.ask("change the timeout") == "Which timeout should I use?"
    assert agent.current_run_state.status == STATUS_WAITING
    assert agent.current_run_state.stop_reason == STOP_REASON_NEEDS_USER_INPUT
    assert agent.current_run_state.tool_steps == 0
    assert not agent._last_tool_result_metadata


def test_repeated_unchanged_reads_use_cache_then_enter_final_only(tmp_path):
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    call = '<tool>{"name":"read_file","args":{"path":"sample.py"}}</tool>'
    agent = make_agent(
        tmp_path,
        [call, call, call, "<final>answered from cached evidence</final>"],
        max_steps=4,
    )

    assert agent.ask("inspect sample.py") == "answered from cached evidence"
    assert agent.current_run_state.tool_steps == 1
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert trace.count('"event": "tool_cache_hit"') == 2
    assert '"reason": "repeated_cached_read"' in trace


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
    child = CodingAgent(
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
    assert "create_task" not in child.tools


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
    agent = CodingAgent.from_session(
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


def test_unchanged_read_cache_does_not_consume_effective_tool_budget(tmp_path):
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    tool_call = '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":5}}</tool>'
    agent = make_agent(tmp_path, outputs=[tool_call, tool_call, tool_call], max_steps=2)

    answer = agent.ask("keep reading until stopped")
    run_state = agent.run_store.load_run_state(agent.current_run_state.run_id)
    report = agent.run_store.load_report(agent.current_run_state.run_id)

    assert answer == "ok"
    assert run_state["status"] == STATUS_COMPLETED
    assert run_state["tool_steps"] == 1
    assert run_state["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED
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


def test_tool_budget_exhaustion_gets_one_final_only_recovery_decision(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    read_a = '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>'
    read_b = '<tool>{"name":"read_file","args":{"path":"b.txt"}}</tool>'
    class TrackingModel(ScriptedModel):
        def __init__(self, outputs):
            super().__init__(outputs)
            self.prompts = []

        def complete(self, request):

            prompt = request.text()
            self.prompts.append(prompt)
            return super().complete(request)

    model = TrackingModel([read_a, read_b, "<final>summary from existing evidence</final>"])
    agent = make_agent(tmp_path, model=model, max_steps=1)

    answer = agent.ask("inspect both if possible")

    assert answer == "summary from existing evidence"
    assert agent.current_run_state.tool_steps == 1
    assert agent.current_run_state.status == STATUS_COMPLETED
    trace = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    assert '"event": "final_only_mode_entered"' in trace
    assert "Runtime directive (authoritative)" in model.prompts[-1]


def test_checkpoint_is_visible_in_the_trailing_reminder(tmp_path):
    agent = make_agent(tmp_path)
    run_state = RunState.create(request_id="req-checkpoint", user_request="resume goal")
    agent.create_checkpoint(run_state, "resume goal", trigger="test")

    request, _ = agent.build_model_request("continue")

    reminder = request.messages[-1]["content"]
    assert "Task checkpoint:" in reminder
    assert "Current goal: resume goal" in reminder
    # 放在末尾：checkpoint 每次都变，不能污染可缓存的 system 前缀。
    assert "Task checkpoint:" not in request.system_prompt


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
