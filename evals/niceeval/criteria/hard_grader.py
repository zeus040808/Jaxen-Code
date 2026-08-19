"""Hidden deterministic grader for HH Agent coding trials."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from hh_agent_pro.evaluation.schema_v2 import classify_trial_v2

from snapshot_workspace import snapshot


def clip(value, limit=4000):
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "...<truncated>"


def changed_paths(before, after):
    before_files = before.get("files", {})
    after_files = after.get("files", {})
    return sorted(
        path
        for path in set(before_files) | set(after_files)
        if before_files.get(path) != after_files.get(path)
    )


def run_checks(task, workspace):
    results = []
    for check in task.get("checks", []):
        if check.get("type") != "command":
            results.append(
                {
                    "id": check.get("id", "unknown"),
                    "passed": False,
                    "error": f"unsupported check type: {check.get('type')}",
                }
            )
            continue
        try:
            completed = subprocess.run(
                check["command"],
                cwd=workspace,
                shell=True,
                executable="/bin/bash",
                capture_output=True,
                text=True,
                timeout=60,
            )
            results.append(
                {
                    "id": check["id"],
                    "passed": completed.returncode == 0,
                    "exit_code": completed.returncode,
                    "stdout": clip(completed.stdout),
                    "stderr": clip(completed.stderr),
                }
            )
        except subprocess.TimeoutExpired as exc:
            results.append(
                {
                    "id": check["id"],
                    "passed": False,
                    "error": "hidden check timed out",
                    "stdout": clip(exc.stdout or ""),
                    "stderr": clip(exc.stderr or ""),
                }
            )
    return results


def trajectory(raw_events):
    tools = []
    by_id = {}
    for event in raw_events:
        if event.get("type") == "operation.started" and event.get("operationType") != "subagent":
            item = {
                "operation_id": event.get("operationId", ""),
                "name": event.get("name", "unknown"),
                "status": "pending",
                "input": event.get("input") or {},
            }
            tools.append(item)
            by_id[event.get("operationId", "")] = item
        elif event.get("type") == "operation.finished":
            item = by_id.get(event.get("operationId", ""))
            if item:
                metadata = event.get("metadata") or {}
                item["status"] = metadata.get("tool_status", "unknown")
                item["error_code"] = metadata.get("error_code", "")
                item["output"] = event.get("output", "")
                if str(item["output"]).startswith("cached observation:"):
                    item["status"] = "cached"
    return {"tools": tools}


def run_trajectory_checks(task, raw_events):
    """Evaluate deterministic process assertions without an LLM judge."""

    trace = trajectory(raw_events)
    tools = trace["tools"]
    results = []
    for index, assertion in enumerate(task.get("trajectory_assertions", [])):
        kind = assertion.get("type")
        check_id = assertion.get("id", f"trajectory_{index}")
        if kind == "tool_call":
            matches = [
                tool for tool in tools
                if tool.get("name") == assertion.get("tool")
                and (
                    not assertion.get("status")
                    or tool.get("status") == assertion.get("status")
                )
            ]
            passed = len(matches) >= int(assertion.get("min_count", 1))
            detail = f"matched {len(matches)} tool calls"
        elif kind == "ordered_tools":
            expected = list(assertion.get("tools", []))
            actual = [tool.get("name") for tool in tools]
            cursor = 0
            for name in actual:
                if cursor < len(expected) and name == expected[cursor]:
                    cursor += 1
            passed = cursor == len(expected)
            detail = f"expected subsequence {expected}; got {actual}"
        elif kind == "event_type":
            count = sum(1 for event in raw_events if event.get("type") == assertion.get("event_type"))
            passed = count >= int(assertion.get("min_count", 1))
            detail = f"matched {count} events"
        elif kind == "no_successful_tool":
            matches = [
                tool for tool in tools
                if tool.get("name") == assertion.get("tool")
                and tool.get("status") in {"ok", "cached", "partial_success"}
            ]
            passed = not matches
            detail = f"successful matches: {len(matches)}"
        else:
            passed = False
            detail = f"unsupported trajectory assertion: {kind}"
        results.append({"id": check_id, "passed": passed, "detail": detail})
    return results


def grade(task, turn_data, before, workspace):
    protocol_turn = turn_data.get("protocolTurn") or {}
    raw_events = turn_data.get("rawEvents") or []
    after = snapshot(workspace)
    paths = changed_paths(before, after)
    checks = run_checks(task, workspace) + run_trajectory_checks(task, raw_events)
    row = {
        "status": protocol_turn.get("status", "failed"),
        "stop_reason": protocol_turn.get("stopReason", ""),
        "final_answer": protocol_turn.get("finalAnswer", ""),
        "checks": checks,
        "changed_paths": paths,
        "trajectory": trajectory(raw_events),
        "tool_steps": sum(
            1 for event in raw_events if event.get("type") == "operation.started"
        ),
    }
    classification = classify_trial_v2(task, row)
    return {
        "schema_version": 2,
        "task_id": task["id"],
        "task_version": task["version"],
        "fixture_digest": before.get("fixture_digest"),
        "changed_paths": paths,
        "checks": checks,
        "row": row,
        **classification,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--before", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--turn", required=True)
    args = parser.parse_args()
    try:
        task = json.loads(Path(args.task).read_text(encoding="utf-8"))
        before = json.loads(Path(args.before).read_text(encoding="utf-8"))
        turn_data = json.loads(Path(args.turn).read_text(encoding="utf-8"))
        expected_digest = task.get("fixture_digest")
        if expected_digest and before.get("fixture_digest") != expected_digest:
            raise ValueError(
                f"fixture digest mismatch: expected {expected_digest}, "
                f"got {before.get('fixture_digest')}"
            )
        print(json.dumps(grade(task, turn_data, before, args.workspace), sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": 2,
                    "trial_status": "grader_error",
                    "automated_gate_pass": False,
                    "grader_error": f"{exc.__class__.__name__}: {exc}",
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
