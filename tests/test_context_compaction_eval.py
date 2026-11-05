"""上下文压缩模块评测作为门禁：每个场景的不变量（预算、协议、目标、最新结果、system、历史）都要满足。"""

from hh_agent_pro.evaluation.context_compaction import run_suite


def test_context_compaction_suite_meets_expectations():
    result = run_suite()
    assert result["summary"]["failures"] == [], [r for r in result["rows"] if r["verdict"] != "pass"]
