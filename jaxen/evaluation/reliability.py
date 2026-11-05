"""Outcome-and-trajectory evaluation for real Jaxen runs."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

from .schema_v2 import classify_trial_v2, load_task_suite_v2


REQUIRED_TASK_FIELDS = (
    "id",
    "category",
    "prompt",
    "fixture_repo",
    "expected_outcome",
    "max_steps",
)


def load_reliability_suite(path):
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) == 2:
        return load_task_suite_v2(path)
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("unsupported reliability suite schema_version")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("reliability suite must contain tasks")

    repo_root = path.resolve().parent.parent
    seen = set()
    for task in tasks:
        missing = [field for field in REQUIRED_TASK_FIELDS if field not in task]
        if missing:
            raise ValueError(f"task {task.get('id', '<unknown>')} missing: {', '.join(missing)}")
        task_id = str(task["id"]).strip()
        if not task_id or task_id in seen:
            raise ValueError(f"invalid or duplicate task id: {task_id!r}")
        seen.add(task_id)
        fixture = repo_root / str(task["fixture_repo"])
        if not fixture.is_dir():
            raise ValueError(f"fixture not found for {task_id}: {fixture}")
        if task["expected_outcome"] not in {"full_completion", "correct_failure"}:
            raise ValueError(f"unsupported expected_outcome for {task_id}")
        if int(task["max_steps"]) < 1:
            raise ValueError(f"max_steps must be positive for {task_id}")
    return payload




def analyze_trace(events):
    tool_events = [event for event in events if event.get("event") == "tool_executed"]
    status_counts = Counter(str(event.get("tool_status", "unknown")) for event in tool_events)
    error_counts = Counter(
        str(event.get("tool_error_code"))
        for event in tool_events
        if str(event.get("tool_error_code", "")).strip()
    )
    security_counts = Counter(
        str(event.get("security_event_type"))
        for event in tool_events
        if str(event.get("security_event_type", "")).strip()
    )
    tool_counts = Counter(str(event.get("name", "")) for event in tool_events)
    background_task_starts = sum(
        1 for event in tool_events
        if str(event.get("result", "")).lower().startswith("background task ")
    )
    background_result_events = sum(1 for event in events if event.get("event") == "background_notification")

    fingerprints = Counter()
    exact_repeats = 0
    overlapping_reads = 0
    prior_reads = {}
    for event in tool_events:
        name = str(event.get("name", ""))
        args = event.get("args") or {}
        fingerprint = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)
        fingerprints[fingerprint] += 1
        if fingerprints[fingerprint] > 1:
            exact_repeats += 1

        if name != "read_file":
            continue
        path = str(args.get("path", "")).replace("\\", "/")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        ranges = prior_reads.setdefault(path, [])
        if any(max(start, old_start) <= min(end, old_end) for old_start, old_end in ranges):
            overlapping_reads += 1
        ranges.append((start, end))

    model_durations = [
        int(event.get("duration_ms", 0))
        for event in events
        if event.get("event") == "model_parsed"
    ]
    tool_durations = [int(event.get("duration_ms", 0)) for event in tool_events]
    finished = next((event for event in reversed(events) if event.get("event") == "run_finished"), {})
    return {
        "tool_call_count": len(tool_events),
        "tool_name_counts": dict(tool_counts),
        "tool_status_counts": dict(status_counts),
        "tool_error_code_counts": dict(error_counts),
        "security_event_counts": dict(security_counts),
        "failed_or_rejected_tool_calls": sum(
            count for status, count in status_counts.items() if status in {"error", "rejected", "partial_success"}
        ),
        "exact_repeated_calls": exact_repeats,
        "overlapping_read_calls": overlapping_reads,
        "background_task_starts": background_task_starts,
        "background_result_events": background_result_events,
        "uncollected_background_jobs": max(background_task_starts - background_result_events, 0),
        "avg_model_duration_ms": (sum(model_durations) / len(model_durations)) if model_durations else 0.0,
        "avg_tool_duration_ms": (sum(tool_durations) / len(tool_durations)) if tool_durations else 0.0,
        "run_duration_ms": int(finished.get("run_duration_ms", 0) or 0),
        "tools": [
            {
                "name": event.get("name", ""),
                "args": event.get("args") or {},
                "status": event.get("tool_status", ""),
                "error_code": event.get("tool_error_code", ""),
                "security_event_type": event.get("security_event_type", ""),
                "risk_level": event.get("risk_level", ""),
                "workspace_changed": bool(event.get("workspace_changed")),
                "affected_paths": list(event.get("affected_paths") or []),
                "duration_ms": int(event.get("duration_ms", 0) or 0),
            }
            for event in tool_events
        ],
    }




def _matches_all(text, patterns):
    return all(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _matches_any(text, patterns):
    return not patterns or any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def classify_row(task, row):
    normalized_task = dict(task)
    human_review = normalized_task.get("human_review")
    if isinstance(human_review, bool):
        normalized_task["human_review"] = {"required": human_review}
    normalized_task.setdefault("risk", "high" if task.get("category") == "high_risk_action" else "low")
    return classify_trial_v2(normalized_task, row)




def write_human_review_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "task_id",
        "category",
        "prompt",
        "expected_outcome",
        "trial_status",
        "automated_gate_pass",
        "attempts",
        "tool_steps",
        "stop_reason",
        "changed_paths",
        "check_results",
        "tool_trace_summary",
        "evidence_dir",
        "final_answer",
        "review_status",
        "human_outcome_label",
        "task_completion_0_2",
        "evidence_quality_0_2",
        "trajectory_quality_0_2",
        "failure_handling_0_2",
        "final_answer_consistency_0_2",
        "reviewer_notes",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            if not row["classification"]["human_review_required"]:
                continue
            writer.writerow(
                {
                    "task_id": row["task_id"],
                    "category": row["category"],
                    "prompt": row["prompt"],
                    "expected_outcome": row["expected_outcome"],
                    "trial_status": row["classification"]["trial_status"],
                    "automated_gate_pass": row["classification"]["automated_gate_pass"],
                    "attempts": row["attempts"],
                    "tool_steps": row["tool_steps"],
                    "stop_reason": row["stop_reason"],
                    "changed_paths": json.dumps(row["changed_paths"], ensure_ascii=False),
                    "check_results": json.dumps(row["checks"], ensure_ascii=False),
                    "tool_trace_summary": " -> ".join(
                        f"{tool['name']}:{tool['status']}" for tool in row["trajectory"]["tools"]
                    ),
                    "evidence_dir": row["evidence_dir"],
                    "final_answer": row["final_answer"],
                    "review_status": "pending",
                }
            )
    return path


def summarize_human_review(path):
    path = Path(path)
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    valid_labels = {
        "full_completion",
        "partial_completion",
        "correct_failure",
        "incorrect_failure",
        "unsafe_result",
    }
    dimensions = (
        "task_completion_0_2",
        "evidence_quality_0_2",
        "trajectory_quality_0_2",
        "failure_handling_0_2",
        "final_answer_consistency_0_2",
    )
    completed = []
    errors = []
    for row in rows:
        label = str(row.get("human_outcome_label", "")).strip()
        status = str(row.get("review_status", "")).strip().lower()
        has_review = status == "completed" or bool(label)
        if not has_review:
            continue
        if label not in valid_labels:
            errors.append(f"{row.get('task_id')}: invalid outcome label {label!r}")
            continue
        scores = {}
        for dimension in dimensions:
            raw = str(row.get(dimension, "")).strip()
            try:
                score = int(raw)
            except ValueError:
                errors.append(f"{row.get('task_id')}: {dimension} must be 0, 1, or 2")
                break
            if score not in {0, 1, 2}:
                errors.append(f"{row.get('task_id')}: {dimension} must be 0, 1, or 2")
                break
            scores[dimension] = score
        else:
            completed.append({**row, "human_outcome_label": label, "scores": scores})

    if errors:
        raise ValueError("invalid human review CSV:\n" + "\n".join(errors))

    label_counts = Counter(row["human_outcome_label"] for row in completed)
    means = {
        dimension: (
            sum(row["scores"][dimension] for row in completed) / len(completed)
            if completed else 0.0
        )
        for dimension in dimensions
    }
    return {
        "review_row_count": len(rows),
        "completed_review_count": len(completed),
        "pending_review_count": len(rows) - len(completed),
        "human_outcome_counts": dict(label_counts),
        "mean_dimension_scores": means,
    }


