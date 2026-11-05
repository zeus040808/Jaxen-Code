"""Persistent project tasks for HH-style agents."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path


VALID_STATUSES = {"pending", "in_progress", "completed", "failed", "blocked", "cancelled"}


@dataclass
class ProjectTask:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] | None = None
    acceptance_criteria: list[str] | None = None
    evidence: list[str] | None = None
    failure_reason: str = ""
    updated_at: str = ""


class ProjectTaskStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / ".hh" / "tasks"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        if not task_id.startswith("task_") or not task_id.replace("_", "").isalnum():
            raise ValueError(f"invalid task id: {task_id}")
        return self.dir / f"{task_id}.json"

    def save(self, task: ProjectTask) -> None:
        if task.status not in VALID_STATUSES:
            raise ValueError(f"invalid task status: {task.status}")
        self._path(task.id).write_text(
            json.dumps(asdict(task), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, task_id: str) -> ProjectTask:
        task = ProjectTask(**json.loads(self._path(task_id).read_text(encoding="utf-8")))
        if task.status not in VALID_STATUSES:
            raise ValueError(f"invalid task status: {task.status}")
        return task

    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> ProjectTask:
        if not subject.strip():
            raise ValueError("subject must not be empty")
        for dependency in blocked_by or []:
            self._path(dependency)
        task = ProjectTask(
            id=f"task_{int(time.time())}_{secrets.token_hex(2)}",
            subject=subject.strip(),
            description=description.strip(),
            blocked_by=list(blocked_by or []),
            acceptance_criteria=[str(item).strip() for item in acceptance_criteria or [] if str(item).strip()],
            evidence=[],
            updated_at=str(time.time()),
        )
        self.save(task)
        return task

    def list(self) -> list[ProjectTask]:
        return [self.load(path.stem) for path in sorted(self.dir.glob("task_*.json"))]

    def can_start(self, task: ProjectTask) -> bool:
        for dependency in task.blocked_by or []:
            path = self._path(dependency)
            if not path.exists() or self.load(dependency).status != "completed":
                return False
        return True

    def claim(self, task_id: str, owner: str = "agent") -> str:
        task = self.load(task_id)
        if task.status != "pending":
            return f"error: {task_id} is {task.status}; cannot claim"
        if not self.can_start(task):
            return f"error: {task_id} is blocked by incomplete dependencies"
        task.status = "in_progress"
        task.owner = owner.strip() or "agent"
        task.updated_at = str(time.time())
        self.save(task)
        return f"claimed {task.id}: {task.subject}"

    def complete(self, task_id: str, evidence: list[str] | None = None) -> str:
        task = self.load(task_id)
        if task.status != "in_progress":
            return f"error: {task_id} is {task.status}; cannot complete"
        task.evidence = list(task.evidence or [])
        task.evidence.extend(str(item).strip() for item in evidence or [] if str(item).strip())
        required = len(task.acceptance_criteria or [])
        if required and len(task.evidence) < required:
            return f"error: {task_id} needs at least {required} evidence item(s) before completion"
        task.status = "completed"
        task.failure_reason = ""
        task.updated_at = str(time.time())
        self.save(task)
        unblocked = [
            item.subject for item in self.list()
            if item.status == "pending" and item.blocked_by and self.can_start(item)
        ]
        suffix = f"\nunblocked: {', '.join(unblocked)}" if unblocked else ""
        return f"completed {task.id}: {task.subject}{suffix}"

    def fail(self, task_id: str, reason: str, evidence: list[str] | None = None) -> str:
        task = self.load(task_id)
        if task.status not in {"pending", "in_progress"}:
            return f"error: {task_id} is {task.status}; cannot fail"
        task.status = "failed"
        task.failure_reason = reason.strip()
        task.evidence = list(task.evidence or []) + [
            str(item).strip() for item in evidence or [] if str(item).strip()
        ]
        task.updated_at = str(time.time())
        self.save(task)
        return f"failed {task.id}: {task.failure_reason}"

    def block(self, task_id: str, reason: str) -> str:
        task = self.load(task_id)
        if task.status not in {"pending", "in_progress"}:
            return f"error: {task_id} is {task.status}; cannot block"
        task.status = "blocked"
        task.failure_reason = reason.strip()
        task.updated_at = str(time.time())
        self.save(task)
        return f"blocked {task.id}: {task.failure_reason}"

    def add_evidence(self, task_id: str, evidence: str) -> str:
        task = self.load(task_id)
        task.evidence = list(task.evidence or [])
        text = evidence.strip()
        if text and text not in task.evidence:
            task.evidence.append(text)
        task.updated_at = str(time.time())
        self.save(task)
        return f"recorded evidence for {task.id}"

    def render(self) -> str:
        tasks = self.list()
        if not tasks:
            return "no project tasks"
        icons = {"pending": " ", "in_progress": ">", "completed": "x", "failed": "!", "blocked": "#", "cancelled": "-"}
        lines = []
        for task in tasks:
            owner = f" owner={task.owner}" if task.owner else ""
            deps = f" blocked_by={','.join(task.blocked_by or [])}" if task.blocked_by else ""
            criteria = f" criteria={len(task.acceptance_criteria or [])}" if task.acceptance_criteria else ""
            evidence = f" evidence={len(task.evidence or [])}" if task.evidence else ""
            reason = f" reason={task.failure_reason}" if task.failure_reason else ""
            lines.append(f"[{icons[task.status]}] {task.id}: {task.subject} ({task.status}){owner}{deps}{criteria}{evidence}{reason}")
        return "\n".join(lines)
