from hh_agent_pro.evaluation.metrics import (
    run_security_experiment_suite,
    run_tool_contract_experiment_suite,
)


def test_security_and_tool_contract_suites_have_disjoint_meaning():
    security = run_security_experiment_suite(repetitions=1)
    tool_contract = run_tool_contract_experiment_suite(repetitions=1)

    security_ids = {row["scenario_id"] for row in security["rows"]}
    tool_contract_ids = {row["scenario_id"] for row in tool_contract["rows"]}

    assert security["pass_rate"] == 1.0
    assert tool_contract["pass_rate"] == 1.0
    assert "repeated_unchanged_read_cache" in tool_contract_ids
    assert "repeated_unchanged_read_cache" not in security_ids
    assert security_ids.isdisjoint(tool_contract_ids)
