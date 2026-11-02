import json
from collections import Counter
from pathlib import Path

from hh_agent_pro.evaluation.schema_v2 import (
    classify_trial_v2,
    fixture_digest,
    load_task_suite_v2,
)
from scripts.eval.build_enterprise_dataset import build_payloads


ROOT = Path(__file__).resolve().parents[1]
ENTERPRISE_PATH = ROOT / "benchmarks/eval_v2/enterprise_tasks_v2.json"
RUNTIME_PATH = ROOT / "benchmarks/eval_v2/runtime_tasks_v2.json"
REFERENCE_PATH = ROOT / "evals/private/reference_trajectories_v2.json"


def test_generated_eval_catalog_has_no_drift():
    generated = build_payloads()
    for relative, expected in generated.items():
        actual = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        assert actual == expected, f"regenerate {relative}"


def test_enterprise_suite_counts_and_coding_distribution():
    suite = load_task_suite_v2(ENTERPRISE_PATH)
    tasks = suite["tasks"]
    assert len(tasks) == 74
    assert Counter(task["suite"] for task in tasks) == {
        "coding-capability": 30,
        "tool-contract": 8,
        "security-regression": 10,
        "session-hitl": 8,
        "robustness": 18,
    }
    assert Counter(
        task["coding_bucket"]
        for task in tasks
        if task["suite"] == "coding-capability"
    ) == {
        "bugfix": 8,
        "feature": 6,
        "multi_file": 5,
        "refactor": 4,
        "test_build_config": 4,
        "read_only_reasoning": 3,
    }
    units = {task["eval_unit"] for task in tasks}
    assert {"response", "step", "episode", "session"} <= units


def test_robustness_variants_count_once_per_logical_group():
    tasks = load_task_suite_v2(ENTERPRISE_PATH)["tasks"]
    robust = [task for task in tasks if task["suite"] == "robustness"]
    groups = Counter(task["aggregation_key"] for task in robust)
    assert groups == {
        "timeout_source": 3,
        "slugify_fix": 3,
        "find_user_missing": 3,
        "subtotal_quantity": 3,
        "port_validator": 3,
        "safe_join_reasoning": 3,
    }


def test_runtime_suite_preserves_fourteen_deterministic_tasks():
    suite = load_task_suite_v2(RUNTIME_PATH)
    assert len(suite["tasks"]) == 14
    assert {task["execution_engine"] for task in suite["tasks"]} == {"python_harness"}


def test_fixture_digests_and_private_reference_coverage():
    tasks = load_task_suite_v2(ENTERPRISE_PATH)["tasks"]
    references = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["trajectories"]
    assert set(references) == {task["id"] for task in tasks}
    for task in tasks:
        assert task["fixture_digest"] == fixture_digest(ROOT / task["fixture_repo"])
        assert task["reference_solution"] == f"private:{task['id']}"
        assert references[task["id"]]["outputs"]
    public_text = ENTERPRISE_PATH.read_text(encoding="utf-8")
    assert "<tool>" not in public_text
    assert "<final>" not in public_text


def test_a_noop_or_empty_answer_cannot_pass_any_enterprise_task():
    tasks = load_task_suite_v2(ENTERPRISE_PATH)["tasks"]
    for task in tasks:
        hard_check_count = len(task["checks"]) + len(task.get("trajectory_assertions", []))
        row = {
            "status": "completed",
            "stop_reason": "final_answer_returned",
            "final_answer": "",
            "checks": [
                {"id": f"wrong_{index}", "passed": False}
                for index in range(hard_check_count)
            ],
            "changed_paths": [],
            "trajectory": {"tools": []},
            "tool_steps": 0,
        }
        result = classify_trial_v2(task, row)
        assert result["automated_gate_pass"] is False, task["id"]
