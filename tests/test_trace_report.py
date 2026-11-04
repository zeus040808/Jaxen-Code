"""复盘汇总脚本（scripts/swebench/trace_report.py）和按运行名分开的 trace 目录。

问题：补丁、失败测试、过程分散在三处，每次复盘都手工拼；评测 trace 按实例存放、不分运行名，
同一道题跑过多次（回归、补跑、验证）时只能猜哪份 trace 属于哪次运行。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "swebench"))
from localization_metrics import instance_trace_dir, main_trace  # noqa: E402
from trace_report import module_counts  # noqa: E402


def write_trace(run_dir, events):
    run_dir.mkdir(parents=True)
    (run_dir / "trace.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_namespaced_trace_dir_wins_over_legacy(tmp_path):
    legacy = tmp_path / "traces" / "iid"
    write_trace(legacy / "runs" / "run_2", [{"event": "run_started", "depth": 0, "request_id": "swe-iid"}])
    assert instance_trace_dir(tmp_path, "run-a", "iid") == legacy  # 旧布局：只有按实例的目录
    namespaced = tmp_path / "traces" / "run-a" / "iid"
    write_trace(namespaced / "runs" / "run_1", [{"event": "run_started", "depth": 0, "request_id": "swe-iid"}])
    assert instance_trace_dir(tmp_path, "run-a", "iid") == namespaced


def test_main_trace_skips_child_runs(tmp_path):
    write_trace(tmp_path / "runs" / "run_1", [{"event": "run_started", "depth": 0}])
    write_trace(tmp_path / "runs" / "run_2", [{"event": "run_started", "depth": 1}])  # 更新，但是审查子 agent
    path, events = main_trace(tmp_path)
    assert path.parent.name == "run_1" and events[0]["depth"] == 0


def test_module_counts():
    events = [
        {"event": "tool_executed", "tool_status": "ok"},
        {"event": "tool_executed", "tool_status": "error", "tool_error_code": "tool_failed"},
        {"event": "review_completed", "verdict": "issues"},
        {"event": "prompt_built", "prompt_metadata": {"compaction": {"kind": "light"}}},
        {"event": "completion_deferred"},
        {"event": "output_budget_raised"},
        {"event": "provider_event", "message": "DeepSeek request failed (HTTP 503); retry 1/5 in 0.8s"},
    ]
    counts = module_counts(events)
    assert counts["tools"] == 2 and counts["tool_fail"] == 1 and counts["fail:tool_failed"] == 1
    assert counts["review:issues"] == 1 and counts["compaction:light"] == 1
    assert counts["completion_deferred"] == 1 and counts["output_budget_raised"] == 1 and counts["http_retry"] == 1
