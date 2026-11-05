"""错误恢复故障注入评测作为门禁：静默失败必须为 0，非已知问题的场景必须全部符合预期。"""

from hh_agent_pro.evaluation.fault_injection import SCENARIOS, run_scenario, run_suite


def test_fault_injection_suite_meets_expectations():
    result = run_suite()
    summary = result["summary"]
    assert summary["silent_failures"] == 0
    assert summary["regular_failures"] == [], [r for r in result["rows"] if r["id"] in summary["regular_failures"]]
    assert not any(row["script_exhausted"] for row in result["rows"] if not row["known_issue"])


def test_scenarios_are_isolated_and_repeatable():
    # 同一场景连跑两次结果一样：每次都是新的临时目录和新的 agent，状态不会串
    spec = next(s for s in SCENARIOS if s["id"] == "net_503_once")
    first, second = run_scenario(spec), run_scenario(spec)
    keys = ("verdict", "outcome", "stop_reason", "model_requests", "changed_files")
    assert {k: first[k] for k in keys} == {k: second[k] for k in keys}
    assert first["changed_files"] == ["calc.py"]
