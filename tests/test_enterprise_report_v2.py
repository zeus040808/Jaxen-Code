import json
from pathlib import Path

from jaxen.evaluation.enterprise_report_v2 import (
    annotate_disagreements_from_niceeval_ndjson,
    build_enterprise_summary,
    load_business_feedback,
    select_human_reviews,
    supplement_trials_from_junit,
    supplement_trials_from_niceeval_ndjson,
    validate_trial_rows,
    write_enterprise_outputs,
)
from jaxen.evaluation.schema_v2 import load_task_suite_v2


ROOT = Path(__file__).resolve().parents[1]
TASKS = load_task_suite_v2(ROOT / "benchmarks/eval_v2/enterprise_tasks_v2.json")["tasks"]


def _rows(profile="precheck", attempts=1):
    rows = []
    for task in TASKS:
        for attempt in range(attempts):
            status = "correct_failure" if task["expected_outcome"] == "correct_failure" else "passed"
            rows.append(
                {
                    "schema_version": 2,
                    "record_id": f"{task['id']}:{attempt}",
                    "run_key": "test-run",
                    "profile": profile,
                    "provider": "fake",
                    "model": "scripted-eval",
                    "task_id": task["id"],
                    "task_version": task["version"],
                    "fixture_digest": task["fixture_digest"],
                    "suite": task["suite"],
                    "suite_type": task["suite_type"],
                    "eval_unit": task["eval_unit"],
                    "risk": task["risk"],
                    "expected_outcome": task["expected_outcome"],
                    "trial_status": status,
                    "automated_gate_pass": True,
                    "valid_capability_trial": True,
                    "approval_preexecution_violations": 0,
                    "usage": {"inputTokens": 10, "outputTokens": 2},
                    "latency_ms": 100,
                }
            )
    return rows


def _deterministic():
    return {
        "platform": "linux",
        "runtime_regression": {"status": "PASS", "pass_rate": 1.0},
        "tool_contract": {"status": "PASS", "pass_rate": 1.0},
        "security_regression": {"status": "PASS", "pass_rate": 1.0, "authoritative": True},
    }


def _summary(rows, profile="precheck", provider="fake", baseline=None):
    return build_enterprise_summary(
        TASKS,
        rows,
        profile=profile,
        provider=provider,
        model="scripted-eval" if provider == "fake" else "deepseek-v4-pro",
        run_key="test-run",
        deterministic=_deterministic(),
        baseline=baseline,
        business=load_business_feedback(),
    )


def test_reference_precheck_is_not_reported_as_agent_capability():
    summary = _summary(_rows())

    assert summary["overall_status"] == "REFERENCE_PRECHECK_PASS"
    assert summary["result_scope"] == "reference_precheck"
    assert summary["business"]["status"] == "NOT_EVALUATED"
    assert summary["coverage"]["backlog_complete_count"] == 74
    assert summary["suites"]["robustness"]["logical_task_count"] == 6


def test_missing_or_infra_trial_makes_run_incomplete():
    missing = _rows()[:-1]
    assert _summary(missing)["overall_status"] == "REFERENCE_PRECHECK_INCOMPLETE"

    infra = _rows(profile="nightly", attempts=3)
    infra[0].update(trial_status="infra_error", automated_gate_pass=False)
    summary = _summary(infra, profile="nightly", provider="deepseek")
    assert summary["overall_status"] == "INCOMPLETE"
    assert summary["coverage"]["backlog_complete_count"] == 73


def test_unsafe_has_fail_precedence():
    rows = _rows(profile="nightly", attempts=3)
    rows[0].update(trial_status="unsafe", automated_gate_pass=False)
    summary = _summary(rows, profile="nightly", provider="deepseek")

    assert summary["overall_status"] == "FAIL"
    assert summary["fatal_counts"]["unsafe"] == 1


def test_trial_validation_rejects_false_positive():
    rows = _rows()
    rows[0].update(trial_status="functional_failed", automated_gate_pass=True)

    try:
        validate_trial_rows(TASKS, rows, "precheck", run_key="test-run")
    except ValueError as exc:
        assert "cannot pass" in str(exc)
    else:
        raise AssertionError("false-positive row was accepted")


def test_junit_supplements_only_missing_sidecar_attempts(tmp_path):
    task = TASKS[0]
    junit = tmp_path / "niceeval.xml"
    junit.write_text(
        '<?xml version="1.0"?><testsuite tests="1"><testcase '
        f'name="enterprise/{task["id"]} [jaxen/deepseek]" time="1.5">'
        '<error message="HTTP 402 insufficient balance"/></testcase></testsuite>',
        encoding="utf-8",
    )

    rows = supplement_trials_from_junit([], junit, TASKS, "nightly", "deepseek", "deepseek-v4-pro", "test-run")

    assert len(rows) == 1
    assert rows[0]["trial_status"] == "infra_error"
    assert rows[0]["latency_ms"] == 1500


def test_killed_run_recovers_provider_errors_from_ndjson(tmp_path):
    task = TASKS[0]
    ndjson = tmp_path / "niceeval.ndjson"
    ndjson.write_text(
        json.dumps(
            {
                "event": "error",
                "locator": "@attempt1",
                "evalId": f"enterprise/{task['id']}",
                "reason": "Jaxen provider/runtime infrastructure error: HTTP 400 reasoning_content missing",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rows = supplement_trials_from_niceeval_ndjson(
        [], ndjson, TASKS, "nightly", "deepseek", "deepseek-v4-pro", "test-run"
    )

    assert len(rows) == 1
    assert rows[0]["trial_status"] == "infra_error"
    assert rows[0]["valid_capability_trial"] is False


def test_niceeval_disagreement_is_added_without_overwriting_python_result(tmp_path):
    rows = _rows()
    task_id = rows[0]["task_id"]
    ndjson = tmp_path / "niceeval.ndjson"
    ndjson.write_text(
        json.dumps({"event": "failure", "evalId": f"enterprise/{task_id}"}) + "\n",
        encoding="utf-8",
    )

    annotated = annotate_disagreements_from_niceeval_ndjson(rows, ndjson)

    assert annotated[0]["automated_gate_pass"] is True
    assert annotated[0]["grader_disagreement"] is True
    assert annotated[0]["valid_alternative_candidate"] is True


def test_human_review_is_additive_and_outputs_are_written(tmp_path):
    rows = _rows()
    rows[0].update(trial_status="unsafe", automated_gate_pass=False)
    reviews = select_human_reviews(TASKS, rows)
    summary = _summary(rows)
    paths = write_enterprise_outputs(tmp_path, summary, reviews)

    assert any(row["review_reason"] == "unsafe" for row in reviews)
    assert summary["trials"]["unsafe_count"] == 1
    assert all(Path(path).is_file() for path in paths.values())
    persisted = json.loads(Path(paths["summary"]).read_text(encoding="utf-8"))
    assert persisted["overall_status"] == "FAIL"
