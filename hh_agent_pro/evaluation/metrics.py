"""从评测结果计算汇总指标和对照报告。

指标只读取运行产物，不决定 Agent 在线执行的下一步。
"""

import json
import hashlib
import tempfile
from datetime import datetime
from pathlib import Path

from ..providers import FakeModelClient
from ..agent import CodingAgent
from ..session_store import SessionStore
from .. import tools as toolkit
from ..workspace import WorkspaceContext

METRICS_SCHEMA_VERSION = 2
DEFAULT_HARNESS_REGRESSION_V2_PATH = Path("artifacts/deterministic/harness-regression-v2.json")
DEFAULT_MEMORY_ABLATION_V2_PATH = Path("artifacts/ablations/memory-ablation-v2.json")
DEFAULT_RECOVERY_ABLATION_V2_PATH = Path("artifacts/ablations/recovery-ablation-v2.json")
DEFAULT_SECURITY_REGRESSION_V1_PATH = Path("artifacts/deterministic/security-regression-v1.json")
DEFAULT_TOOL_CONTRACT_V1_PATH = Path("artifacts/deterministic/tool-contract-v1.json")
DEFAULT_CORE_REPORT_PATH = Path("docs/results/benchmark-core-report.md")


def _safe_mean(values):
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def _safe_ratio(numerator, denominator):
    if not denominator:
        return 0.0
    return numerator / denominator






class _MemoryExperimentModelClient(FakeModelClient):
    def __init__(self, expected_fact, filename):
        super().__init__([])
        self.expected_fact = str(expected_fact).strip().lower()
        self.filename = str(filename).strip()
        self.phase = "bootstrap_tool"
        self.followup_reads = 0

    def complete(self, request):
        prompt = request.text()
        self.prompts.append(prompt)
        self.last_completion_metadata = {}
        if self.phase == "bootstrap_tool":
            self.phase = "bootstrap_final"
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'
        if self.phase == "bootstrap_final":
            self.phase = "question"
            return "<final>Done.</final>"
        if self.phase == "question":
            # 长期记忆位于 system prompt：只在那里查，不把对话里读到的文件内容当成记忆。
            system_lower = request.system_prompt.lower()
            memory_view = system_lower.split("curated durable memory", 1)[1] if "curated durable memory" in system_lower else ""
            if self.expected_fact in memory_view:
                return f"<final>{self.expected_fact.capitalize()}.</final>"
            self.phase = "question_after_read"
            self.followup_reads += 1
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.filename}","start":1,"end":20}}}}</tool>'
        if self.phase == "question_after_read":
            self.phase = "done"
            return f"<final>{self.expected_fact.capitalize()}.</final>"
        return f"<final>{self.expected_fact.capitalize()}.</final>"


def _build_memory_experiment_agent(workspace_root, expected_fact, filename):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".hh" / "sessions")
    return CodingAgent(
        model_client=_MemoryExperimentModelClient(expected_fact, filename),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def _set_irrelevant_memory(agent):
    agent.curated_memory_snapshot = "Curated durable memory (benchmark noise):\n- team mascot is blue"


def _inject_curated_fact(agent, fact):
    agent.curated_memory.apply(
        {
            "action": "add",
            "namespace": "memory",
            "scope": "workspace",
            "kind": "fact",
            "content": str(fact),
            "provenance": "verified_tool",
            "confidence": 1.0,
        }
    )
    agent.curated_memory_snapshot = agent.curated_memory.render_snapshot()




MEMORY_EXPERIMENT_TASKS = [
    {"id": "fact_color", "category": "fact_lookup", "filename": "facts.txt", "fact": "deploy key is red"},
    {"id": "fact_api", "category": "fact_lookup", "filename": "settings.txt", "fact": "api base path is /v1/internal"},
    {"id": "fact_budget", "category": "fact_lookup", "filename": "limits.txt", "fact": "default step budget is 6"},
    {"id": "fact_timeout", "category": "fact_lookup", "filename": "runtime.txt", "fact": "timeout ceiling is 120 seconds"},
    {"id": "edit_intro", "category": "edit_dependency", "filename": "README.md", "fact": "first bullet is the locked intro line"},
    {"id": "edit_token", "category": "edit_dependency", "filename": "sample.txt", "fact": "second token is placeholder"},
    {"id": "edit_field", "category": "edit_dependency", "filename": "config.txt", "fact": "fixed field name is benchmark_schema"},
    {"id": "edit_line", "category": "edit_dependency", "filename": "notes.txt", "fact": "locked marker is on line three"},
    {"id": "history_file", "category": "history_reference", "filename": "history.txt", "fact": "deploy fact came from facts.txt"},
    {"id": "history_line", "category": "history_reference", "filename": "history.txt", "fact": "benchmark note came from line two"},
    {"id": "history_token", "category": "history_reference", "filename": "history.txt", "fact": "placeholder token was beta"},
    {"id": "history_tool", "category": "history_reference", "filename": "history.txt", "fact": "inspection tool was read_file"},
]


def _write_memory_task_files(workspace_root, task):
    filename = task["filename"]
    payload = task["fact"]
    (workspace_root / filename).write_text(payload + "\n", encoding="utf-8")


def _bootstrap_prompt(task):
    return f"Read {task['filename']} and remember the key fact."


def _followup_prompt(task):
    if task["category"] == "fact_lookup":
        return f"What does {task['filename']} say?"
    if task["category"] == "edit_dependency":
        return f"Use the remembered constraint from {task['filename']} to continue without rereading."
    return f"What was the conclusion we already established from {task['filename']}?"


def _set_irrelevant_memory_for_task(agent):
    _set_irrelevant_memory(agent)


def _run_memory_task_variant(task, variant):
    with tempfile.TemporaryDirectory(prefix="hh-memory-large-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        _write_memory_task_files(workspace_root, task)
        agent = _build_memory_experiment_agent(workspace_root, task["fact"], task["filename"])
        assert agent.ask(_bootstrap_prompt(task)) == "Done."
        _inject_curated_fact(agent, task["fact"])
        if variant == "memory_off":
            agent.feature_flags["memory"] = False
        elif variant == "memory_irrelevant":
            _set_irrelevant_memory_for_task(agent)
        result = agent.ask(_followup_prompt(task))
        run_state = agent.current_run_state
        return {
            "correct": result.strip().lower() == f"{task['fact']}.",
            "tool_steps": int(run_state.tool_steps),
            "attempts": int(run_state.attempts),
            "repeated_reads": int(getattr(agent.model_client, "followup_reads", 0)),
        }


def run_large_scale_memory_experiment(repetitions=5):
    repetitions = int(repetitions)
    variants = {
        "memory_on": [],
        "memory_off": [],
        "memory_irrelevant": [],
    }
    for task in MEMORY_EXPERIMENT_TASKS:
        for _ in range(repetitions):
            for variant in variants:
                row = _run_memory_task_variant(task, variant)
                row["task_id"] = task["id"]
                row["category"] = task["category"]
                variants[variant].append(row)
    category_counts = {}
    for task in MEMORY_EXPERIMENT_TASKS:
        category_counts[task["category"]] = category_counts.get(task["category"], 0) + 1
    return {
        "task_count": len(MEMORY_EXPERIMENT_TASKS),
        "runs_per_variant": len(MEMORY_EXPERIMENT_TASKS) * repetitions,
        "category_counts": category_counts,
        "variants": {
            variant: {
                "repeated_reads": sum(row["repeated_reads"] for row in rows),
                "avg_tool_steps": _safe_mean(row["tool_steps"] for row in rows),
                "avg_attempts": _safe_mean(row["attempts"] for row in rows),
                "correct_rate": _safe_ratio(sum(1 for row in rows if row["correct"]), len(rows)),
                "memory_hit_rate": _safe_ratio(sum(1 for row in rows if row["repeated_reads"] == 0), len(rows)),
            }
            for variant, rows in variants.items()
        },
        "rows": variants,
    }




def _security_agent(workspace_root, approval_policy="auto", read_only=False):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".hh" / "sessions")
    return CodingAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        read_only=read_only,
    )


def _scenario_invalid_patch_nonunique(workspace_root):
    (workspace_root / "sample.txt").write_text("beta\nbeta\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    agent.run_tool("patch_file", {"path": "sample.txt", "old_text": "beta", "new_text": "locked"})
    return dict(agent._last_tool_result_metadata)


def _scenario_invalid_patch_missing_field(workspace_root):
    (workspace_root / "sample.txt").write_text("beta\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    agent.run_tool("patch_file", {"path": "sample.txt", "old_text": "beta"})
    return dict(agent._last_tool_result_metadata)


def _scenario_timeout_out_of_range(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("run_shell", {"command": "echo hi", "timeout": toolkit.MAX_SHELL_TIMEOUT + 1})
    return dict(agent._last_tool_result_metadata)


def _scenario_empty_command(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("run_shell", {"command": "", "timeout": 20})
    return dict(agent._last_tool_result_metadata)


def _scenario_empty_delegate_task(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("delegate", {"task": "", "max_steps": 2})
    return dict(agent._last_tool_result_metadata)


def _scenario_path_escape_read(workspace_root):
    outside = workspace_root.parent / f"{workspace_root.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    agent.run_tool("read_file", {"path": "../outside.txt"})
    return dict(agent._last_tool_result_metadata)


def _scenario_symlink_escape(workspace_root):
    outside = workspace_root.parent / f"{workspace_root.name}-symlink-target.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (workspace_root / "linked.txt").symlink_to(outside)
    agent = _security_agent(workspace_root)
    agent.run_tool("read_file", {"path": "linked.txt"})
    return dict(agent._last_tool_result_metadata)


def _scenario_search_escape(workspace_root):
    agent = _security_agent(workspace_root)
    agent.run_tool("search", {"pattern": "abc", "path": "../outside"})
    return dict(agent._last_tool_result_metadata)


def _scenario_approval_denied(workspace_root):
    agent = _security_agent(workspace_root, approval_policy="never")
    agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})
    return dict(agent._last_tool_result_metadata)


def _scenario_read_only_block(workspace_root):
    agent = _security_agent(workspace_root, read_only=True)
    agent.run_tool("write_file", {"path": "x.txt", "content": "nope"})
    return dict(agent._last_tool_result_metadata)


def _scenario_repeated_call(workspace_root):
    (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
    agent = _security_agent(workspace_root)
    args = {"path": "README.md", "start": 1, "end": 1}
    for _ in range(2):
        result = agent.run_tool("read_file", args)
        agent.record({"role": "tool", "name": "read_file", "args": args, "content": result, "created_at": "2026-04-09T00:00:00+00:00"})
    agent.run_tool("read_file", args)
    return dict(agent._last_tool_result_metadata)


SECURITY_SCENARIOS = [
    ("path_escape_read", _scenario_path_escape_read),
    ("symlink_escape", _scenario_symlink_escape),
    ("search_escape", _scenario_search_escape),
    ("approval_denied_shell", _scenario_approval_denied),
    ("read_only_write", _scenario_read_only_block),
]

TOOL_CONTRACT_SCENARIOS = [
    ("repeated_unchanged_read_cache", _scenario_repeated_call),
    ("patch_nonunique", _scenario_invalid_patch_nonunique),
    ("patch_missing_new_text", _scenario_invalid_patch_missing_field),
    ("timeout_out_of_range", _scenario_timeout_out_of_range),
    ("empty_shell_command", _scenario_empty_command),
    ("empty_delegate_task", _scenario_empty_delegate_task),
]

SECURITY_EXPECTATIONS = {
    "path_escape_read": ("path_escape", "path_escape"),
    "symlink_escape": ("path_escape", "path_escape"),
    "search_escape": ("path_escape", "path_escape"),
    "approval_denied_shell": ("approval_denied", "approval_denied"),
    "read_only_write": ("approval_denied", "read_only_block"),
}

TOOL_CONTRACT_EXPECTATIONS = {
    "patch_nonunique": ("invalid_arguments", ""),
    "patch_missing_new_text": ("invalid_arguments", ""),
    "timeout_out_of_range": ("invalid_arguments", ""),
    "empty_shell_command": ("invalid_arguments", ""),
    "empty_delegate_task": ("invalid_arguments", ""),
}


def run_security_experiment_suite(repetitions=3):
    repetitions = int(repetitions)
    rows = []
    security_event_counts = {}
    tool_error_code_counts = {}
    skipped = 0
    for scenario_id, runner in SECURITY_SCENARIOS:
        for _ in range(repetitions):
            with tempfile.TemporaryDirectory(prefix="hh-security-exp-") as temp_dir:
                workspace_root = Path(temp_dir)
                (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
                try:
                    metadata = runner(workspace_root)
                except OSError as exc:
                    skipped += 1
                    rows.append(
                        {
                            "scenario_id": scenario_id,
                            "skipped": True,
                            "skip_reason": str(exc),
                            "passed": False,
                        }
                    )
                    continue
                metadata["scenario_id"] = scenario_id
                expected_error, expected_event = SECURITY_EXPECTATIONS[scenario_id]
                metadata["skipped"] = False
                metadata["passed"] = (
                    metadata.get("tool_error_code") == expected_error
                    and (not expected_event or metadata.get("security_event_type") == expected_event)
                )
                rows.append(metadata)
                event = str(metadata.get("security_event_type", "")).strip()
                if event:
                    security_event_counts[event] = security_event_counts.get(event, 0) + 1
                error_code = str(metadata.get("tool_error_code", "")).strip()
                if error_code:
                    tool_error_code_counts[error_code] = tool_error_code_counts.get(error_code, 0) + 1
    return {
        "scenario_count": len(SECURITY_SCENARIOS),
        "runs": len(rows),
        "executed_runs": len(rows) - skipped,
        "skipped_runs": skipped,
        "passed_runs": sum(1 for row in rows if row.get("passed")),
        "pass_rate": _safe_ratio(
            sum(1 for row in rows if row.get("passed")),
            len(rows) - skipped,
        ),
        "security_event_counts": security_event_counts,
        "tool_error_code_counts": tool_error_code_counts,
        "rows": rows,
    }


def run_security_regression_v1(artifact_path=DEFAULT_SECURITY_REGRESSION_V1_PATH, repetitions=3):
    repetitions = int(repetitions)
    payload = run_security_experiment_suite(repetitions=repetitions)
    artifact = {
        "schema_version": 1,
        "artifact_type": "security-regression-v1",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "repetitions": repetitions,
        **payload,
    }
    return _write_json_artifact(artifact_path, artifact)


def run_tool_contract_experiment_suite(repetitions=3):
    """Exercise tool semantics separately from security policy decisions."""

    rows = []
    for scenario_id, runner in TOOL_CONTRACT_SCENARIOS:
        for _ in range(int(repetitions)):
            with tempfile.TemporaryDirectory(prefix="hh-tool-contract-") as temp_dir:
                metadata = runner(Path(temp_dir))
                metadata["scenario_id"] = scenario_id
                metadata["skipped"] = False
                if scenario_id == "repeated_unchanged_read_cache":
                    metadata["passed"] = (
                        metadata.get("tool_status") == "ok"
                        and metadata.get("cache_hit") is True
                        and metadata.get("effective_tool_step") is False
                        and int(metadata.get("cache_hit_count", 0)) >= 2
                    )
                else:
                    expected_error, expected_event = TOOL_CONTRACT_EXPECTATIONS[scenario_id]
                    metadata["passed"] = (
                        metadata.get("tool_error_code") == expected_error
                        and (
                            not expected_event
                            or metadata.get("security_event_type") == expected_event
                        )
                    )
                rows.append(metadata)
    passed = sum(1 for row in rows if row.get("passed"))
    return {
        "scenario_count": len(TOOL_CONTRACT_SCENARIOS),
        "runs": len(rows),
        "executed_runs": len(rows),
        "skipped_runs": 0,
        "passed_runs": passed,
        "pass_rate": _safe_ratio(passed, len(rows)),
        "rows": rows,
    }


def run_tool_contract_regression_v1(
    artifact_path=DEFAULT_TOOL_CONTRACT_V1_PATH,
    repetitions=3,
):
    payload = run_tool_contract_experiment_suite(repetitions=repetitions)
    artifact = {
        "schema_version": 1,
        "artifact_type": "tool-contract-v1",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "repetitions": int(repetitions),
        **payload,
    }
    return _write_json_artifact(artifact_path, artifact)




def _write_json_artifact(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


class _RecoveryScenarioModelClient(FakeModelClient):
    def __init__(self, required_fragments, success_answer):
        super().__init__([])
        self.required_fragments = [str(fragment).lower() for fragment in required_fragments]
        self.success_answer = str(success_answer)

    def complete(self, request):
        prompt = request.text()
        self.prompts.append(prompt)
        self.last_completion_metadata = {}
        prompt_lower = prompt.lower()
        if all(fragment in prompt_lower for fragment in self.required_fragments):
            return f"<final>{self.success_answer}</final>"
        return "<final>missing recovery state.</final>"


RECOVERY_ABLATION_TASKS = [
    {
        "id": "checkpoint_resume_goal",
        "category": "checkpoint_resume",
        "setup": "checkpoint_resume",
        "required_fragments": ["task checkpoint:", "current goal: resume the benchmark task", "next step: apply the locked change"],
    },
    {
        "id": "checkpoint_resume_files",
        "category": "checkpoint_resume",
        "setup": "checkpoint_resume",
        "required_fragments": ["task checkpoint:", "current goal: continue from the latest benchmark checkpoint", "key files: sample.txt"],
    },
    {
        "id": "partial_stale_single",
        "category": "partial_stale",
        "setup": "partial_stale_single",
        "required_fragments": ["resume status: partial-stale", "stale paths: sample.txt"],
    },
    {
        "id": "partial_stale_multi",
        "category": "partial_stale",
        "setup": "partial_stale_multi",
        "required_fragments": ["resume status: partial-stale", "stale paths: sample.txt, notes.txt"],
    },
    {
        "id": "workspace_mismatch_fingerprint",
        "category": "workspace_mismatch",
        "setup": "workspace_mismatch",
        "required_fragments": ["resume status: workspace-mismatch", "current goal: recover after workspace drift"],
    },
    {
        "id": "workspace_mismatch_runtime",
        "category": "workspace_mismatch",
        "setup": "workspace_mismatch",
        "required_fragments": ["resume status: workspace-mismatch", "next step: rebuild runtime state from a fresh checkpoint"],
    },
    {
        "id": "schema_mismatch_version",
        "category": "schema_mismatch",
        "setup": "schema_mismatch",
        "required_fragments": ["resume status: schema-mismatch"],
    },
    {
        "id": "schema_mismatch_missing",
        "category": "schema_mismatch",
        "setup": "no_checkpoint",
        "required_fragments": ["current user request:", "continue the recovery task."],
    },
    {
        "id": "partial_success_shell",
        "category": "partial_success_recovery",
        "setup": "partial_success_shell",
        "required_fragments": ["current blocker: tool_partial_success", "next step: inspect the diff before retry"],
    },
    {
        "id": "partial_success_tool",
        "category": "partial_success_recovery",
        "setup": "partial_success_tool",
        "required_fragments": ["current blocker: tool_failed", "next step: retry after checking the workspace state"],
    },
]


def _build_recovery_agent(workspace_root, required_fragments):
    workspace = WorkspaceContext.build(workspace_root)
    store = SessionStore(workspace_root / ".hh" / "sessions")
    return CodingAgent(
        model_client=_RecoveryScenarioModelClient(required_fragments, "recovery state restored."),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=4,
    )


def _apply_recovery_setup(agent, task, workspace_root):
    setup = task["setup"]
    workspace_root = Path(workspace_root)
    (workspace_root / "sample.txt").write_text("alpha\nbeta\ngamma\nplaceholder\n", encoding="utf-8")
    (workspace_root / "notes.txt").write_text("note-one\nnote-two\n", encoding="utf-8")
    agent.session["memory"] = agent.memory.to_dict()

    if setup == "checkpoint_resume":
        agent.memory.remember_file("sample.txt")
        agent.session["memory"] = agent.memory.to_dict()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_resume",
            "items": {
                "ckpt_resume": {
                    "checkpoint_id": "ckpt_resume",
                    "parent_checkpoint_id": "",
                    "schema_version": "phase1-v1",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Resume the benchmark task" if task["id"] == "checkpoint_resume_goal" else "Continue from the latest benchmark checkpoint",
                    "completed": ["Read sample.txt"],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Apply the locked change" if task["id"] == "checkpoint_resume_goal" else "Continue from remembered file anchors",
                    "key_files": [{"path": "sample.txt", "freshness": None}],
                    "freshness": {},
                    "summary": "checkpoint resume benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        if task["id"] == "checkpoint_resume_files":
            agent.session["checkpoints"]["items"]["ckpt_resume"]["key_files"] = [{"path": "sample.txt", "freshness": None}]
        agent.session_store.save(agent.session)
        return

    if setup in {"partial_stale_single", "partial_stale_multi"}:
        agent.memory.remember_file("sample.txt")
        sample_freshness = hashlib.sha256((agent.root / "sample.txt").read_bytes()).hexdigest()
        key_files = [{"path": "sample.txt", "freshness": sample_freshness}]
        freshness = {"sample.txt": sample_freshness}
        if setup == "partial_stale_multi":
            agent.memory.remember_file("notes.txt")
            notes_freshness = hashlib.sha256((agent.root / "notes.txt").read_bytes()).hexdigest()
            key_files.append({"path": "notes.txt", "freshness": notes_freshness})
            freshness["notes.txt"] = notes_freshness
        agent.session["memory"] = agent.memory.to_dict()
        agent.session["checkpoints"] = {
            "current_id": "ckpt_stale",
            "items": {
                "ckpt_stale": {
                    "checkpoint_id": "ckpt_stale",
                    "parent_checkpoint_id": "",
                    "schema_version": "phase1-v1",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover from stale benchmark summaries",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Re-anchor the stale summaries",
                    "key_files": key_files,
                    "freshness": freshness,
                    "summary": "partial stale benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        agent.session_store.save(agent.session)
        (workspace_root / "sample.txt").write_text("alpha\nbeta\nstale-shifted\nplaceholder\n", encoding="utf-8")
        if setup == "partial_stale_multi":
            (workspace_root / "notes.txt").write_text("note-one\nnote-two-shifted\n", encoding="utf-8")
        return

    if setup == "workspace_mismatch":
        agent.session["checkpoints"] = {
            "current_id": "ckpt_workspace",
            "items": {
                "ckpt_workspace": {
                    "checkpoint_id": "ckpt_workspace",
                    "parent_checkpoint_id": "",
                    "schema_version": "phase1-v1",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover after workspace drift",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Rebuild runtime state from a fresh checkpoint",
                    "key_files": [],
                    "freshness": {},
                    "summary": "workspace mismatch benchmark",
                    "runtime_identity": {"workspace_fingerprint": "outdated-workspace-fingerprint"},
                }
            },
        }
        agent.session_store.save(agent.session)
        return

    if setup == "schema_mismatch":
        agent.session["checkpoints"] = {
            "current_id": "ckpt_schema",
            "items": {
                "ckpt_schema": {
                    "checkpoint_id": "ckpt_schema",
                    "parent_checkpoint_id": "",
                    "schema_version": "legacy-v0",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover after schema mismatch",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": "",
                    "next_step": "Migrate the stale checkpoint",
                    "key_files": [],
                    "freshness": {},
                    "summary": "schema mismatch benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        agent.session_store.save(agent.session)
        return

    if setup == "no_checkpoint":
        agent.session.pop("checkpoints", None)
        agent.session_store.save(agent.session)
        return

    if setup in {"partial_success_shell", "partial_success_tool"}:
        blocker = "tool_partial_success" if setup == "partial_success_shell" else "tool_failed"
        next_step = "Inspect the diff before retry" if setup == "partial_success_shell" else "Retry after checking the workspace state"
        agent.session["checkpoints"] = {
            "current_id": "ckpt_partial",
            "items": {
                "ckpt_partial": {
                    "checkpoint_id": "ckpt_partial",
                    "parent_checkpoint_id": "",
                    "schema_version": "phase1-v1",
                    "created_at": "2026-04-15T08:00:00+00:00",
                    "current_goal": "Recover after partial tool success",
                    "completed": [],
                    "excluded": [],
                    "current_blocker": blocker,
                    "next_step": next_step,
                    "key_files": [{"path": "sample.txt", "freshness": None}],
                    "freshness": {},
                    "summary": "partial success benchmark",
                    "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
                }
            },
        }
        agent.session_store.save(agent.session)


def _run_recovery_task_variant(task, variant):
    with tempfile.TemporaryDirectory(prefix="hh-recovery-ablation-") as temp_dir:
        workspace_root = Path(temp_dir)
        (workspace_root / "README.md").write_text("demo\n", encoding="utf-8")
        agent = _build_recovery_agent(workspace_root, task["required_fragments"])
        _apply_recovery_setup(agent, task, workspace_root)
        if variant == "resume_disabled":
            agent.session.pop("checkpoints", None)
            agent.session_store.save(agent.session)
        final_answer = agent.ask("Continue the recovery task.")
        report = agent.run_store.load_report(agent.current_run_state.run_id)
        trace = [
            json.loads(line)
            for line in agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8").splitlines()
        ]
        resume_status = str(report.get("prompt_metadata", {}).get("resume_status", ""))
        stale_reanchored = any(
            event.get("event") == "checkpoint_created" and event.get("trigger") == "freshness_mismatch"
            for event in trace
        )
        workspace_drift_detected = any(event.get("event") == "runtime_identity_mismatch" for event in trace)
        invalid_resume = task["category"] in {"partial_stale", "workspace_mismatch", "schema_mismatch"}
        return {
            "task_id": task["id"],
            "category": task["category"],
            "variant": variant,
            "resume_status": resume_status,
            "resume_succeeded": final_answer == "recovery state restored.",
            "stale_reanchored": stale_reanchored,
            "workspace_drift_detected": workspace_drift_detected,
            "false_accept": invalid_resume and resume_status == "full-valid",
            "final_answer": final_answer,
        }


def _recovery_variant_summary(rows):
    rows = list(rows)
    stale_rows = [row for row in rows if row["category"] == "partial_stale"]
    drift_rows = [row for row in rows if row["category"] == "workspace_mismatch"]
    invalid_rows = [row for row in rows if row["category"] in {"partial_stale", "workspace_mismatch", "schema_mismatch"}]
    return {
        "resume_success_rate": _safe_ratio(sum(1 for row in rows if row["resume_succeeded"]), len(rows)),
        "stale_reanchor_rate": _safe_ratio(sum(1 for row in stale_rows if row["stale_reanchored"]), len(stale_rows)),
        "workspace_drift_detection_rate": _safe_ratio(sum(1 for row in drift_rows if row["workspace_drift_detected"]), len(drift_rows)),
        "resume_false_accept_rate": _safe_ratio(sum(1 for row in invalid_rows if row["false_accept"]), len(invalid_rows)),
    }




def run_memory_ablation_v2(artifact_path=DEFAULT_MEMORY_ABLATION_V2_PATH, repetitions=5):
    payload = run_large_scale_memory_experiment(repetitions=repetitions)
    artifact = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "memory-ablation-v2",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "task_count": payload["task_count"],
        "runs_per_variant": payload["runs_per_variant"],
        "category_counts": payload["category_counts"],
        "variants": payload["variants"],
        "rows": payload["rows"],
    }
    return _write_json_artifact(artifact_path, artifact)


def run_recovery_ablation_v2(artifact_path=DEFAULT_RECOVERY_ABLATION_V2_PATH, repetitions=3):
    repetitions = int(repetitions)
    variants = {"resume_enabled": [], "resume_disabled": []}
    for task in RECOVERY_ABLATION_TASKS:
        for _ in range(repetitions):
            for variant in variants:
                variants[variant].append(_run_recovery_task_variant(task, variant))
    artifact = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "recovery-ablation-v2",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "repetitions": repetitions,
        "task_count": len(RECOVERY_ABLATION_TASKS),
        "variants": {
            variant: {
                "summary": _recovery_variant_summary(rows),
                "rows": rows,
            }
            for variant, rows in variants.items()
        },
    }
    return _write_json_artifact(artifact_path, artifact)


def write_benchmark_core_report(
    report_path=DEFAULT_CORE_REPORT_PATH,
    harness_artifact_path=DEFAULT_HARNESS_REGRESSION_V2_PATH,
    memory_artifact_path=DEFAULT_MEMORY_ABLATION_V2_PATH,
    recovery_artifact_path=DEFAULT_RECOVERY_ABLATION_V2_PATH,
    security_artifact_path=DEFAULT_SECURITY_REGRESSION_V1_PATH,
):
    harness = json.loads(Path(harness_artifact_path).read_text(encoding="utf-8"))
    memory = json.loads(Path(memory_artifact_path).read_text(encoding="utf-8"))
    recovery = json.loads(Path(recovery_artifact_path).read_text(encoding="utf-8"))
    security = json.loads(Path(security_artifact_path).read_text(encoding="utf-8"))

    enabled_recovery = recovery["variants"]["resume_enabled"]["summary"]
    lines = [
        "# HH Benchmark Core Report",
        "",
        "这轮 benchmark 覆盖 Harness regression、working memory ablation、recovery ablation 和 security regression；不把真实 Provider 能力与 deterministic runtime 指标混写。",
        "",
        "## Harness Regression",
        f"- 固定 regression 任务数：{harness['summary']['total_tasks']}",
        f"- pass_rate：{harness['summary']['pass_rate']:.2%}",
        f"- within_budget_rate：{harness['summary']['within_budget_rate']:.2%}",
        f"- verifier_pass_rate：{harness['summary']['verifier_pass_rate']:.2%}",
        "",
        "## Working Memory Ablation",
        f"- 任务类型数：{memory['task_count']}",
        f"- 每个变体样本数：{memory['runs_per_variant']}",
        f"- memory_on repeated_reads：{memory['variants']['memory_on']['repeated_reads']}",
        f"- memory_off repeated_reads：{memory['variants']['memory_off']['repeated_reads']}",
        f"- memory_on avg_tool_steps：{memory['variants']['memory_on']['avg_tool_steps']:.2f}",
        f"- memory_off avg_tool_steps：{memory['variants']['memory_off']['avg_tool_steps']:.2f}",
        f"- memory_on correct_rate：{memory['variants']['memory_on']['correct_rate']:.2%}",
        f"- memory_hit_rate：{memory['variants']['memory_on']['memory_hit_rate']:.2%}",
        "",
        "## Recovery / Resume Ablation",
        f"- 恢复场景数：{recovery['task_count']}",
        f"- resume_enabled 样本数：{len(recovery['variants']['resume_enabled']['rows'])}",
        f"- resume_success_rate：{enabled_recovery['resume_success_rate']:.2%}",
        f"- stale_reanchor_rate：{enabled_recovery['stale_reanchor_rate']:.2%}",
        f"- workspace_drift_detection_rate：{enabled_recovery['workspace_drift_detection_rate']:.2%}",
        f"- resume_false_accept_rate：{enabled_recovery['resume_false_accept_rate']:.2%}",
        "",
        "## Security Regression",
        f"- 场景数：{security['scenario_count']}",
        f"- 每场景计划重复：{security.get('repetitions', '-')}",
        f"- 已执行样本：{security['executed_runs']}",
        f"- 跳过样本：{security['skipped_runs']}",
        f"- pass_rate：{security['pass_rate']:.2%}",
        "",
        "## 可以安全写进简历的指标",
        "- avg_full_prompt_chars",
        "- avg_raw_prompt_chars",
        "- avg_prompt_compression_ratio",
        "- max_prompt_compression_ratio",
        "- repeated_reads",
        "- avg_tool_steps",
        "- correct_rate",
        "- resume_success_rate",
        "- workspace_drift_detection_rate",
        "- resume_false_accept_rate",
        "- security pass_rate",
        "",
        "## 只适合放文档/面试展开的指标",
        "- current_request_preserved_rate",
        "- memory_hit_rate",
        "- stale_reanchor_rate",
        "- failure_category_counts",
        "",
        "## 口径边界",
        "- 所有 Core Report 数据均来自 scripted deterministic model，证明 Runtime 合同和模块收益，不代表真实模型智能上限。",
        "- Harness regression 只证明 runtime 合同稳定，不证明 provider 上限。",
        "- Context、memory、recovery 这三层只证明模块收益，不和 provider benchmark 混写。",
    ]
    report_text = "\n".join(lines) + "\n"
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    return report_text
