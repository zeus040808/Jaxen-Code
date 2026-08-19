"""聚合 trial sidecar，按 suite 给出能力、可靠性与安全指标。

与 ``enterprise_report_v2.py`` 的分工：那个模块面向完整的发布门禁流程，
需要 NiceEval NDJSON、JUnit、确定性产物等一整套输入；本脚本只读
``trial-results-*.jsonl``，用来在评测进行中或部分完成时快速看清现状。

关键口径（与 ``schema_v2`` 一致）：

* ``infra_error`` / ``grader_error`` / ``skipped`` 不进能力分母。
* ``agent_protocol_error`` **进**分母——请求是 Agent 自己拼错的。
* ``pass^k`` 定义为「k 次 trial 全部通过门禁的任务数 / 拿到 k 次 trial 的任务数」。

用法::

    python scripts/summarize_trial_results.py
    python scripts/summarize_trial_results.py --path artifacts/eval-v2/xxx.jsonl --json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hh_agent_pro.evaluation.schema_v2 import EXCLUDED_FROM_CAPABILITY_DENOMINATOR

DEFAULT_PATH = Path("artifacts/eval-v2/trial-results-nightly-v2.jsonl")


def load_rows(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _usage_total(rows, key):
    return sum(int((row.get("usage") or {}).get(key, 0)) for row in rows)


def summarize_suite(rows, target_trials=3):
    valid = [
        row for row in rows
        if row["trial_status"] not in EXCLUDED_FROM_CAPABILITY_DENOMINATOR
    ]
    by_task = defaultdict(list)
    for row in rows:
        by_task[row["task_id"]].append(row)

    complete_tasks = {
        task: trials for task, trials in by_task.items() if len(trials) >= target_trials
    }
    pass_k_tasks = sum(
        1 for trials in complete_tasks.values()
        if all(row["automated_gate_pass"] for row in trials)
    )
    input_tokens = _usage_total(rows, "inputTokens")
    latencies = [int(row.get("latency_ms", 0)) for row in rows]

    return {
        "trials": len(rows),
        "tasks": len(by_task),
        "valid_capability_trials": len(valid),
        "automated_gate_pass": sum(1 for row in valid if row["automated_gate_pass"]),
        "capability_pass_rate": (
            sum(1 for row in valid if row["automated_gate_pass"]) / len(valid)
            if valid else 0.0
        ),
        "tasks_with_full_trials": len(complete_tasks),
        "pass_k_tasks": pass_k_tasks,
        "pass_k_rate": pass_k_tasks / len(complete_tasks) if complete_tasks else None,
        "status_counts": dict(Counter(row["trial_status"] for row in rows)),
        "safety_violations": sum(1 for row in rows if row.get("safety_violation")),
        "approval_preexecution": sum(
            int(row.get("approval_preexecution_violations", 0)) for row in rows
        ),
        "trials_with_changes": sum(1 for row in rows if row.get("changed_paths")),
        "input_tokens": input_tokens,
        "output_tokens": _usage_total(rows, "outputTokens"),
        "cache_read_tokens": _usage_total(rows, "cacheReadTokens"),
        "cache_hit_rate": (
            _usage_total(rows, "cacheReadTokens") / input_tokens if input_tokens else 0.0
        ),
        "mean_latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
    }


def failing_checks(rows):
    """按失败的 check id 归类，用来快速判断失败是否集中于同一原因。"""

    reasons = Counter()
    for row in rows:
        if row["automated_gate_pass"]:
            continue
        for check in row.get("checks") or []:
            if not check.get("passed"):
                detail = str(check.get("detail") or "").strip()
                reasons[f"{check.get('id')} :: {detail[:48]}"] += 1
    return reasons


def render(rows, target_trials=3):
    lines = []
    by_suite = defaultdict(list)
    for row in rows:
        by_suite[row["suite"]].append(row)

    lines.append(f"总计 {len(rows)} trials，覆盖 {len({r['task_id'] for r in rows})} 个任务\n")
    header = f"{'suite':22s} {'trials':>6s} {'tasks':>5s} {'门禁':>9s} {'pass^k':>9s} {'unsafe':>6s} {'cache':>6s}"
    lines.append(header)
    lines.append("-" * len(header))

    for suite in sorted(by_suite):
        s = summarize_suite(by_suite[suite], target_trials)
        pass_k = "n/a" if s["pass_k_rate"] is None else f"{s['pass_k_tasks']}/{s['tasks_with_full_trials']}"
        lines.append(
            f"{suite:22s} {s['trials']:>6d} {s['tasks']:>5d} "
            f"{s['automated_gate_pass']:>4d}/{s['valid_capability_trials']:<4d} "
            f"{pass_k:>9s} {s['safety_violations']:>6d} {s['cache_hit_rate']*100:>5.1f}%"
        )

    total = summarize_suite(rows, target_trials)
    lines.append("-" * len(header))
    lines.append(
        f"{'ALL':22s} {total['trials']:>6d} {total['tasks']:>5d} "
        f"{total['automated_gate_pass']:>4d}/{total['valid_capability_trials']:<4d} "
        f"{'':>9s} {total['safety_violations']:>6d} {total['cache_hit_rate']*100:>5.1f}%"
    )

    lines.append("\n零容忍项")
    lines.append(f"  safety_violation      : {total['safety_violations']}")
    lines.append(f"  批准前误执行           : {total['approval_preexecution']}")

    lines.append("\n各 suite 状态分布")
    for suite in sorted(by_suite):
        s = summarize_suite(by_suite[suite], target_trials)
        lines.append(f"  {suite:22s} {s['status_counts']}")

    lines.append("\n门禁失败原因归类（按 check id）")
    reasons = failing_checks(rows)
    if not reasons:
        lines.append("  无")
    for reason, count in reasons.most_common(12):
        lines.append(f"  {count:>3d}x  {reason}")

    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--target-trials", type=int, default=3)
    parser.add_argument("--json", action="store_true", help="输出机器可读汇总")
    args = parser.parse_args(argv)

    rows = load_rows(args.path)
    if args.json:
        by_suite = defaultdict(list)
        for row in rows:
            by_suite[row["suite"]].append(row)
        payload = {
            "source": args.path,
            "target_trials": args.target_trials,
            "overall": summarize_suite(rows, args.target_trials),
            "suites": {
                suite: summarize_suite(items, args.target_trials)
                for suite, items in by_suite.items()
            },
            "failing_checks": dict(failing_checks(rows)),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(rows, args.target_trials))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
