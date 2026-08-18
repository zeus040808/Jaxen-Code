"""Persistent semantic progress for the current user task.

RunState counts attempts and tools. TaskState answers the higher-level
questions: what is the goal, which explicit subtasks remain, what evidence was
collected, and which failures or claims are still unresolved.
"""

from __future__ import annotations

import re
import uuid

from .workspace import clip, now


TASK_STATE_SCHEMA_VERSION = "v1"
EVIDENCE_LIMIT = 60
FAILURE_LIMIT = 30
CLAIM_LIMIT = 30


def default_task_state():
    return {
        "schema_version": TASK_STATE_SCHEMA_VERSION,
        "task_id": "",
        "goal": "",
        "status": "idle",
        "run_ids": [],
        "continuation_requests": [],
        "subtask_ids": [],
        "evidence": [],
        "claims": [],
        "open_questions": [],
        "failures": [],
        "next_action": "",
        "created_at": "",
        "updated_at": now(),
    }


class TaskState:
    CONTINUABLE_STATUSES = {"running", "waiting_for_user", "paused"}

    def __init__(self, state, task_store):
        self.state = state if isinstance(state, dict) else default_task_state()
        self.task_store = task_store
        self._ensure_shape()

    def _ensure_shape(self):
        defaults = default_task_state()
        for key, value in defaults.items():
            if key not in self.state:
                self.state[key] = list(value) if isinstance(value, list) else value
        for key in (
            "run_ids",
            "continuation_requests",
            "subtask_ids",
            "evidence",
            "claims",
            "open_questions",
            "failures",
        ):
            if not isinstance(self.state.get(key), list):
                self.state[key] = []
        self.state["schema_version"] = TASK_STATE_SCHEMA_VERSION

    def to_dict(self):
        return self.state

    def _touch(self):
        self.state["updated_at"] = now()

    def _reset(self, goal, run_id):
        self.state.clear()
        self.state.update(default_task_state())
        self.state.update(
            {
                "task_id": "taskrun_" + uuid.uuid4().hex[:10],
                "goal": clip(str(goal).strip(), 1000),
                "status": "running",
                "run_ids": [str(run_id)],
                "created_at": now(),
                "updated_at": now(),
            }
        )

    def begin(self, user_message, run_id):
        user_message = str(user_message).strip()
        if self.state.get("status") not in self.CONTINUABLE_STATUSES or not self.state.get("goal"):
            self._reset(user_message, run_id)
            return

        if user_message and user_message != self.state.get("goal"):
            self.state["continuation_requests"].append(clip(user_message, 500))
            self.state["continuation_requests"] = self.state["continuation_requests"][-10:]
        self.state["status"] = "running"
        self.state["run_ids"].append(str(run_id))
        self.state["run_ids"] = list(dict.fromkeys(self.state["run_ids"]))[-20:]
        if self.state["open_questions"]:
            self._append_evidence(
                {
                    "kind": "user_followup",
                    "summary": clip(user_message, 300),
                    "created_at": now(),
                }
            )
            self.state["open_questions"] = []
        self._touch()

    def _append_evidence(self, evidence):
        self.state["evidence"].append(evidence)
        self.state["evidence"] = self.state["evidence"][-EVIDENCE_LIMIT:]

    @staticmethod
    def _task_id_from_result(result):
        match = re.search(r"\b(task_[A-Za-z0-9_]+)\b", str(result))
        return match.group(1) if match else ""

    def observe_tool(self, name, args, result, metadata):
        metadata = dict(metadata or {})
        status = str(metadata.get("tool_status", "unknown"))
        error_code = str(metadata.get("tool_error_code", ""))
        task_id = str((args or {}).get("task_id", "")).strip()
        if name == "create_task" and status == "ok":
            task_id = self._task_id_from_result(result)
        if task_id and task_id not in self.state["subtask_ids"]:
            self.state["subtask_ids"].append(task_id)

        self._append_evidence(
            {
                "kind": "tool",
                "tool": str(name),
                "status": status,
                "error_code": error_code,
                "summary": clip(str(result), 350),
                "affected_paths": list(metadata.get("affected_paths", [])),
                "created_at": now(),
            }
        )

        if status == "ok":
            for failure in self.state["failures"]:
                if failure.get("retryable") and not failure.get("resolved"):
                    failure["resolved"] = True
                    failure["resolved_at"] = now()
                    failure["resolution"] = f"followed by successful {name}"
            self.state["next_action"] = "Review the evidence and choose the next required action."
        else:
            fingerprint = str(metadata.get("failure_fingerprint", ""))
            self.state["failures"].append(
                {
                    "tool": str(name),
                    "error_code": error_code or status,
                    "fingerprint": fingerprint,
                    "retryable": bool(metadata.get("retryable", False)),
                    "resolved": False,
                    "summary": clip(str(result), 350),
                    "created_at": now(),
                }
            )
            self.state["failures"] = self.state["failures"][-FAILURE_LIMIT:]
            self.state["next_action"] = str(metadata.get("recommended_action", "")).strip()
        self._touch()

    def record_claim(self, claim, evidence, verified=True, task_id=""):
        claim_text = clip(str(claim).strip(), 500)
        task_id = str(task_id).strip()
        if verified:
            for previous in reversed(self.state["claims"]):
                if previous.get("claim") == claim_text and previous.get("task_id", "") == task_id:
                    previous["evidence"] = clip(str(evidence).strip(), 700)
                    previous["verified"] = True
                    previous["verified_at"] = now()
                    self._append_evidence(
                        {
                            "kind": "claim",
                            "claim": claim_text,
                            "summary": previous["evidence"],
                            "verified": True,
                            "task_id": task_id,
                            "created_at": now(),
                        }
                    )
                    self._touch()
                    return "updated claim as verified"
        item = {
            "claim": claim_text,
            "evidence": clip(str(evidence).strip(), 700),
            "verified": bool(verified),
            "task_id": task_id,
            "created_at": now(),
        }
        self.state["claims"].append(item)
        self.state["claims"] = self.state["claims"][-CLAIM_LIMIT:]
        if item["task_id"] and item["task_id"] not in self.state["subtask_ids"]:
            self.state["subtask_ids"].append(item["task_id"])
        self._append_evidence({"kind": "claim", "summary": item["evidence"], **item})
        self._touch()
        return "recorded verified claim" if verified else "recorded unverified claim"

    def record_background_notification(self, notification):
        self._append_evidence(
            {
                "kind": "background",
                "summary": clip(str(notification), 500),
                "created_at": now(),
            }
        )
        self._touch()

    def mark_waiting(self, question):
        self.state["status"] = "waiting_for_user"
        question = clip(str(question).strip(), 500)
        if question and question not in self.state["open_questions"]:
            self.state["open_questions"].append(question)
        self.state["next_action"] = question
        self._touch()

    def mark_paused(self, reason, next_action=""):
        self.state["status"] = "paused"
        self.state["next_action"] = clip(str(next_action or reason), 500)
        self._touch()

    def mark_blocked(self, reason):
        self.state["status"] = "blocked"
        self.state["next_action"] = clip(str(reason), 500)
        self._touch()

    def mark_completed(self, final_answer):
        self.state["status"] = "completed"
        self.state["next_action"] = ""
        self._append_evidence(
            {"kind": "delivery", "summary": clip(str(final_answer), 500), "created_at": now()}
        )
        self._touch()

    def _subtasks(self):
        tasks = []
        for task_id in self.state["subtask_ids"]:
            try:
                tasks.append(self.task_store.load(task_id))
            except (FileNotFoundError, ValueError, TypeError):
                continue
        return tasks

    def delivery_blockers(self, background_pending=0):
        blockers = []
        if background_pending:
            blockers.append(f"{background_pending} background task(s) are still running")

        for task in self._subtasks():
            if task.status in {"pending", "in_progress"}:
                blockers.append(f"subtask {task.id} is {task.status}")
            elif task.status in {"failed", "blocked"}:
                reason = task.failure_reason or "no reason recorded"
                blockers.append(f"subtask {task.id} is {task.status}: {reason}")

        for claim in self.state["claims"]:
            if not claim.get("verified"):
                blockers.append(f"claim is not verified: {claim.get('claim', '')}")

        for question in self.state["open_questions"]:
            blockers.append(f"open question: {question}")

        for failure in self.state["failures"]:
            if not failure.get("resolved") and failure.get("retryable"):
                blockers.append(
                    f"unresolved failure {failure.get('tool')}:{failure.get('error_code')}"
                )
        return list(dict.fromkeys(blockers))

    def completed_summaries(self):
        return [f"{task.id}: {task.subject}" for task in self._subtasks() if task.status == "completed"]

    def blocker_summary(self):
        blockers = self.delivery_blockers()
        return " | ".join(blockers[:4])

    def render(self):
        lines = [
            "Task progress:",
            f"- Goal: {self.state.get('goal') or '-'}",
            f"- Status: {self.state.get('status') or 'idle'}",
            f"- Next action: {self.state.get('next_action') or '-'}",
        ]
        tasks = self._subtasks()
        if tasks:
            lines.append("- Subtasks:")
            for task in tasks[-12:]:
                reason = f" reason={task.failure_reason}" if task.failure_reason else ""
                lines.append(f"  - {task.id}: {task.subject} [{task.status}]{reason}")
        else:
            lines.append("- Subtasks: none recorded")

        unresolved = [item for item in self.state["failures"] if not item.get("resolved")]
        lines.append(
            "- Unresolved failures: "
            + (" | ".join(f"{item.get('tool')}:{item.get('error_code')}" for item in unresolved[-4:]) or "none")
        )
        unverified = [item.get("claim", "") for item in self.state["claims"] if not item.get("verified")]
        lines.append("- Unverified claims: " + (" | ".join(unverified[-4:]) or "none"))
        lines.append(
            "- Open questions: " + (" | ".join(self.state["open_questions"][-4:]) or "none")
        )
        recent_evidence = self.state["evidence"][-5:]
        lines.append("- Recent evidence:")
        if recent_evidence:
            lines.extend(f"  - {item.get('kind')}: {item.get('summary', '')}" for item in recent_evidence)
        else:
            lines.append("  - none")
        return "\n".join(lines)
