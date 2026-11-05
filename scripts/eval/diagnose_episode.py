"""跑一个真实 coding 任务并解剖它的决策轨迹。

回答一个具体问题：**模型请求次数远多于工具步数时，多出来的请求去哪了？**

评测只给出「17 次请求 / 8 步预算 / 170 秒」这样的聚合数字，看不出是
格式重试、缓存命中、交付门禁打回，还是并行工具调用被丢弃。本脚本直接读
trace.jsonl，把每一次模型请求归类，并输出耗时构成。

用法::

    python scripts/eval/diagnose_episode.py --task coding_bugfix_parse_bool
    python scripts/eval/diagnose_episode.py --task ... --max-steps 16 --timeout-ms 600000
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jaxen.cli import _build_model_client
from jaxen.config import load_project_env
from jaxen.run_store import RunStore
from jaxen.agent import CodingAgent, SessionStore
from jaxen.workspace import WorkspaceContext

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = REPO_ROOT / "benchmarks" / "eval_v2" / "enterprise_tasks_v2.json"


def load_task(task_id):
    payload = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    for task in payload["tasks"]:
        if task["id"] == task_id:
            return task
    raise SystemExit(f"未找到任务：{task_id}")


def classify_requests(events):
    """把每次模型请求归类到它最终产生的决策。"""

    decisions = []
    for event in events:
        if event.get("event") == "model_decision":
            decisions.append(event)
    kinds = Counter(str(d.get("kind", "?")) for d in decisions)
    tool_names = Counter(
        str(d.get("name", "")) for d in decisions if d.get("kind") == "tool"
    )
    return kinds, tool_names


def summarize(events):
    by_event = Counter(str(e.get("event", "")) for e in events)
    kinds, tool_names = classify_requests(events)

    parallel_returned = 0
    parallel_discarded = 0
    for event in events:
        if event.get("event") != "model_parsed":
            continue
        meta = event.get("completion_metadata") or {}
        parallel_returned += int(meta.get("native_tool_calls_returned", 0) or 0)
        parallel_discarded += int(meta.get("native_tool_calls_discarded", 0) or 0)

    model_ms = sum(
        int(e.get("duration_ms", 0) or 0) for e in events if e.get("event") == "model_parsed"
    )
    tool_ms = sum(
        int(e.get("duration_ms", 0) or 0) for e in events if e.get("event") == "tool_executed"
    )
    prompt_ms = sum(
        int(e.get("duration_ms", 0) or 0) for e in events if e.get("event") == "prompt_built"
    )
    finished = next(
        (e for e in reversed(events) if e.get("event") == "run_finished"), {}
    )

    cache_hits = by_event.get("tool_cache_hit", 0)
    deferred = by_event.get("completion_deferred", 0)
    rejected = by_event.get("model_output_rejected", 0)

    # 门禁反复驳回时，真正要知道的是"卡在哪一条"，而不是"驳回了几次"。
    blocker_counts = Counter()
    for event in events:
        if event.get("event") == "completion_deferred":
            for blocker in event.get("blockers") or []:
                blocker_counts[str(blocker)] += 1

    return {
        "model_requests": len(kinds and [e for e in events if e.get("event") == "model_decision"]),
        "decision_kinds": dict(kinds),
        "tool_calls_executed": by_event.get("tool_executed", 0),
        "tool_name_counts": dict(tool_names),
        "native_tool_calls_returned": parallel_returned,
        "native_tool_calls_discarded": parallel_discarded,
        "format_retries": rejected,
        "delivery_deferrals": deferred,
        "tool_cache_hits": cache_hits,
        "blocker_counts": dict(blocker_counts),
        "checkpoints_created": by_event.get("checkpoint_created", 0),
        "model_time_ms": model_ms,
        "tool_time_ms": tool_ms,
        "prompt_build_time_ms": prompt_ms,
        "run_duration_ms": int(finished.get("run_duration_ms", 0) or 0),
        "status": finished.get("status", ""),
        "stop_reason": finished.get("stop_reason", ""),
    }


def render(summary, task):
    budgets = task["budgets"]
    lines = []
    lines.append(f"任务          {task['id']}  ({task['category']})")
    lines.append(f"预算          max_steps={budgets['max_steps']}  "
                 f"max_new_tokens={budgets['max_new_tokens']}  timeout={budgets['timeout_ms']}ms")
    lines.append("")
    lines.append("--- 决策构成 ---")
    lines.append(f"  模型请求总数        {summary['model_requests']}")
    for kind, count in sorted(summary["decision_kinds"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    {kind:22s} {count}")
    lines.append("")
    lines.append("--- 请求去向（非工具步的开销）---")
    lines.append(f"  实际执行工具        {summary['tool_calls_executed']}")
    lines.append(f"  格式重试            {summary['format_retries']}")
    lines.append(f"  交付门禁打回        {summary['delivery_deferrals']}")
    lines.append(f"  读缓存命中(不计步)  {summary['tool_cache_hits']}")
    if summary.get("blocker_counts"):
        lines.append("")
        lines.append("--- 门禁卡在哪一条 ---")
        for blocker, count in sorted(summary["blocker_counts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:>3d}x  {blocker}")
    lines.append("")
    lines.append("--- 并行工具调用 ---")
    lines.append(f"  后端返回 tool_call  {summary['native_tool_calls_returned']}")
    lines.append(f"  被丢弃              {summary['native_tool_calls_discarded']}  "
                 f"{'← 每丢弃 1 个就多一轮往返' if summary['native_tool_calls_discarded'] else ''}")
    lines.append("")
    lines.append("--- 耗时构成 ---")
    total = max(summary["run_duration_ms"], 1)
    for label, key in (("模型等待", "model_time_ms"), ("工具执行", "tool_time_ms"),
                       ("prompt 组装", "prompt_build_time_ms")):
        value = summary[key]
        lines.append(f"  {label:12s} {value/1000:7.1f}s  ({100*value/total:4.1f}%)")
    lines.append(f"  {'总计':12s} {total/1000:7.1f}s")
    lines.append("")
    lines.append(f"结局          status={summary['status']}  stop_reason={summary['stop_reason']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default="coding_bugfix_parse_bool")
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--max-steps", type=int, default=None, help="覆盖任务预算，用于对照实验")
    parser.add_argument("--max-new-tokens", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    load_project_env(REPO_ROOT)
    task = load_task(args.task)
    budgets = task["budgets"]

    with tempfile.TemporaryDirectory(prefix="jaxen-diagnose-") as temp_dir:
        workspace_root = Path(temp_dir) / Path(task["fixture_repo"]).name
        shutil.copytree(REPO_ROOT / task["fixture_repo"], workspace_root)

        workspace = WorkspaceContext.build(workspace_root, repo_root_override=workspace_root)
        agent = CodingAgent(
            model_client=_build_model_client(argparse.Namespace(
                provider=args.provider, model=None, base_url=None, temperature=0.0,
                request_timeout=120, context_window=None,
            )),
            workspace=workspace,
            session_store=SessionStore(workspace_root / ".jaxen" / "sessions"),
            run_store=RunStore(workspace_root / ".jaxen" / "runs"),
            approval_policy=str(task.get("approval_policy", "auto")),
            read_only=bool(task.get("read_only", False)),
            max_steps=int(args.max_steps or budgets["max_steps"]),
            max_new_tokens=int(args.max_new_tokens or budgets["max_new_tokens"]),
        )

        started = time.monotonic()
        try:
            agent.ask(task["prompt"])
        except Exception as exc:
            print(f"运行异常：{exc.__class__.__name__}: {exc}", file=sys.stderr)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        trace_path = agent.run_store.trace_path(agent.current_run_state)
        events = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    summary = summarize(events)
    summary.setdefault("wall_clock_ms", elapsed_ms)
    if args.json:
        print(json.dumps({"task": task["id"], "summary": summary}, ensure_ascii=False, indent=2))
    else:
        print(render(summary, task))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
