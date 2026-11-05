"""复盘汇总：从一次评测运行的 trace 里统计各模块触发了多少次，或者把一道题的过程拼成一页时间线。

    # 整批：每题一行 + 合计（同时写出 <运行名>.trace-report.json）
    python3 scripts/swebench/trace_report.py --run-name hh-flash-medium20d-review --out-dir artifacts/swebench

    # 单题：版本配置、逐步工具调用、关键事件、失败的隐藏测试、transcript 位置
    python3 scripts/swebench/trace_report.py --run-name hh-flash-medium20d-review --out-dir artifacts/swebench \\
        --instance sympy__sympy-23413

补丁在 predictions、失败的测试在判分日志、过程在 trace，以前每次复盘都要手工拼；这个脚本只读这三处，不调用模型。
"""

import argparse
import collections
import json
from pathlib import Path

from localization_metrics import instance_trace_dir, main_trace

# 值得在时间线里单独标出来的事件（其余如 prompt_built、checkpoint_created 只计数不展示）
NOTABLE = {
    "completion_deferred", "completion_forced", "final_only_mode_entered", "model_output_truncated",
    "output_recovery_started", "output_budget_raised", "model_output_rejected", "internal_tool_protocol_rejected",
    "model_failed", "review_completed",
}


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def module_counts(events):
    """把一次运行的 trace 折算成各模块的触发次数。"""
    counts = collections.Counter()
    for event in events:
        name = event.get("event")
        if name == "tool_executed":
            counts["tools"] += 1
            if event.get("tool_status") != "ok":
                counts["tool_fail"] += 1
                counts["fail:" + (event.get("tool_error_code") or "unknown")] += 1
        elif name == "review_completed":
            counts["review:" + event.get("verdict", "?")] += 1
        elif name == "prompt_built":
            compaction = (event.get("prompt_metadata") or {}).get("compaction")
            if compaction:
                counts["compaction:" + compaction.get("kind", "?")] += 1
        elif name == "model_parsed":
            counts["empty_retries"] += int((event.get("completion_metadata") or {}).get("empty_response_retries", 0) or 0)
        elif name == "provider_event" and "retry" in str(event.get("message", "")):
            counts["http_retry"] += 1
        elif name in NOTABLE:
            counts[name] += 1
    return counts


def failing_tests(out_dir, run_name, instance_id):
    """官方判分报告里没通过的测试（FAIL_TO_PASS 和 PASS_TO_PASS 两类）。"""
    pattern = f"harness/logs/run_evaluation/{run_name}-{instance_id}/*/{instance_id}/report.json"
    for report in Path(out_dir).glob(pattern):
        status = json.loads(report.read_text(encoding="utf-8")).get(instance_id, {}).get("tests_status", {})
        return {kind: status.get(kind, {}).get("failure", []) for kind in ("FAIL_TO_PASS", "PASS_TO_PASS")}
    return {}


def batch(args):
    out_dir = Path(args.out_dir)
    rows, total = [], collections.Counter()
    for result in read_jsonl(out_dir / f"{args.run_name}.results.jsonl"):
        instance_id = result["instance_id"]
        _, events = main_trace(instance_trace_dir(out_dir, args.run_name, instance_id))
        counts = module_counts(events)
        total.update(counts)
        rows.append({"instance_id": instance_id, "outcome": result.get("outcome"),
                     "stop_reason": result.get("stop_reason"), "counts": dict(counts)})
        shown = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()) if key != "tools")
        print(f"{instance_id:38} {result.get('outcome', ''):11} {str(result.get('stop_reason') or ''):24} "
              f"tools={counts['tools']:<3} {shown}")
    print("TOTAL", json.dumps(dict(sorted(total.items())), ensure_ascii=False))
    (out_dir / f"{args.run_name}.trace-report.json").write_text(
        json.dumps({"total": dict(total), "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")


def timeline(args):
    out_dir = Path(args.out_dir)
    trace_path, events = main_trace(instance_trace_dir(out_dir, args.run_name, args.instance))
    if not events:
        print(f"no main-agent trace for {args.instance}")
        return
    started = events[0]
    print(f"# {args.instance}  ({trace_path.parent.name})")
    print(f"model={started.get('model', '?')} version={started.get('agent_version', '?')} "
          f"config={json.dumps(started.get('config', {}), ensure_ascii=False)}")
    step = 0
    for event in events:
        name = event.get("event")
        if name == "tool_executed":
            step += 1
            status = event.get("tool_status")
            mark = "" if status == "ok" else f"  <-- {event.get('tool_error_code') or status}"
            print(f"{step:3}. {event.get('name')} {json.dumps(event.get('args', {}), ensure_ascii=False)[:110]}{mark}")
        elif name in NOTABLE:
            detail = {k: v for k, v in event.items() if k not in ("event", "created_at", "usage")}
            print(f"     ** {name} {json.dumps(detail, ensure_ascii=False)[:200]}")
        elif name == "run_finished":
            print(f"END status={event.get('status')} stop_reason={event.get('stop_reason')}")
    failures = failing_tests(out_dir, args.run_name, args.instance)
    if failures:
        print("failing hidden tests:", json.dumps(failures, ensure_ascii=False))
    transcript = trace_path.parent / "transcript.jsonl"
    print("transcript:", transcript if transcript.exists() else "(not recorded for this run)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--instance", help="只看这一道题的时间线")
    args = parser.parse_args()
    timeline(args) if args.instance else batch(args)


if __name__ == "__main__":
    main()
