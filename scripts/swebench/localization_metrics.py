"""代码定位指标：拿官方补丁涉及的文件当标准答案，从 agent 的 trace 里看它多快找到、找得准不准。

    steps_to_gold       第几次工具调用第一次读到正确文件（越小越好）
    files_before_gold   读到正确文件之前读了几个无关文件（绕了多少弯）
    gold_read_recall    官方补丁涉及的文件里，agent 读过的比例
    edit_recall         应该改的文件里，agent 改了的比例
    edit_precision      agent 改的文件里，确实该改的比例（衡量"顺手多改"）

    .venv-swebench/bin/python scripts/swebench/localization_metrics.py \\
        --run-name hh-flash-easy20 --out-dir artifacts/swebench
"""

import argparse
import json
import re
import statistics
from pathlib import Path

DATASET = "SWE-bench/SWE-bench_Verified"
READ_TOOLS = {"read_file", "read_files"}


def patch_files(patch):
    return [path for path in re.findall(r"^diff --git a/(\S+)", patch or "", re.M)]


def normalize(path):
    path = str(path or "").strip()
    return path[len("/testbed/"):] if path.startswith("/testbed/") else path.lstrip("./")


def read_paths(event):
    args = event.get("args") or {}
    paths = args.get("paths") if event.get("name") == "read_files" else [args.get("path")]
    return [normalize(path) for path in (paths or []) if path]


def is_main_agent_run(events):
    """子 agent（调查/审查）的 run 和主 agent 存在同一个目录里：只取主 agent 的。
    新 trace 看 run_started 的 depth；旧 trace 没有 depth：评测里主 agent 的 request_id 是 "swe-<实例>"，
    子 agent 用的是自动生成的 "req_…"。"""
    started = next((event for event in events if event.get("event") == "run_started"), {})
    if "depth" in started:
        return started["depth"] == 0
    return not str(started.get("request_id", "")).startswith("req_")


def instance_trace_dir(out_dir, run_name, instance_id):
    """trace 目录：新布局按运行名分开（traces/<运行名>/<实例>）；旧布局所有运行混在 traces/<实例> 里，
    同一道题跑过多次时只能取最新的一次（2026-11-03 之前的运行都是旧布局）。"""
    namespaced = Path(out_dir) / "traces" / run_name / instance_id
    return namespaced if namespaced.exists() else Path(out_dir) / "traces" / instance_id


def main_trace(trace_dir):
    """一次运行里主 agent 的全部 trace 事件（多次运行时取最新一次）。"""
    for trace in sorted(Path(trace_dir).glob("runs/*/trace.jsonl"), reverse=True):
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
        if is_main_agent_run(events):
            return trace, events
    return None, []


def tool_calls(trace_dir):
    """按顺序返回一次运行里主 agent 的工具调用（多次运行时取最新一次）。"""
    _, events = main_trace(trace_dir)
    return [event for event in events if event.get("event") == "tool_executed"]


def measure(gold, predicted, calls):
    gold = set(gold)
    steps_to_gold, read_before, seen = None, [], set()
    for index, call in enumerate(calls, start=1):
        if call.get("name") not in READ_TOOLS:
            continue
        paths = read_paths(call)
        if steps_to_gold is None and gold & set(paths):
            steps_to_gold = index
        if steps_to_gold is None:
            read_before += [path for path in paths if path not in seen]
        seen.update(paths)
    edited = set(predicted)
    return {
        "steps_to_gold": steps_to_gold,
        "files_before_gold": len(set(read_before)),
        "gold_read_recall": len(gold & seen) / len(gold) if gold else None,
        "edit_recall": len(gold & edited) / len(gold) if gold else None,
        "edit_precision": len(gold & edited) / len(edited) if edited else None,
        "tool_calls": len(calls),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    from datasets import load_dataset

    out_dir = Path(args.out_dir)
    predictions = {
        row["instance_id"]: row["model_patch"]
        for row in map(json.loads, (out_dir / f"{args.run_name}.predictions.jsonl").read_text(encoding="utf-8").splitlines())
    }
    gold = {x["instance_id"]: patch_files(x["patch"]) for x in load_dataset(DATASET, split="test") if x["instance_id"] in predictions}

    rows = []
    for instance_id in sorted(predictions):
        row = {"instance_id": instance_id, "gold_files": gold[instance_id],
               **measure(gold[instance_id], patch_files(predictions[instance_id]),
                         tool_calls(instance_trace_dir(out_dir, args.run_name, instance_id)))}
        rows.append(row)
        print(f"{instance_id:34} steps_to_gold={str(row['steps_to_gold']):>4}  detour_files={row['files_before_gold']:>2}  "
              f"read_recall={row['gold_read_recall']:.2f}  edit_recall={row['edit_recall']:.2f}  "
              f"edit_precision={(row['edit_precision'] or 0):.2f}  tool_calls={row['tool_calls']}")

    def median(key):
        values = [row[key] for row in rows if row[key] is not None]
        return statistics.median(values) if values else None

    summary = {
        "instances": len(rows),
        "found_gold": sum(row["steps_to_gold"] is not None for row in rows),
        "median_steps_to_gold": median("steps_to_gold"),
        "median_files_before_gold": median("files_before_gold"),
        "mean_gold_read_recall": statistics.mean(row["gold_read_recall"] for row in rows),
        "mean_edit_recall": statistics.mean(row["edit_recall"] for row in rows),
        "mean_edit_precision": statistics.mean(row["edit_precision"] or 0 for row in rows),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    (out_dir / f"{args.run_name}.localization.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
