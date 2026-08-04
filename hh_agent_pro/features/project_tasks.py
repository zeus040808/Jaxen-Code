"""Persistent project tasks for HH-style agents."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path


VALID_STATUSES = {"pending", "in_progress", "completed"}


@dataclass
class ProjectTask:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] | None = None


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

    def create(self, subject: str, description: str = "", blocked_by: list[str] | None = None) -> ProjectTask:
        if not subject.strip():
            raise ValueError("subject must not be empty")
        for dependency in blocked_by or []:
            self._path(dependency)
        task = ProjectTask(
            id=f"task_{int(time.time())}_{secrets.token_hex(2)}",
            subject=subject.strip(),
            description=description.strip(),
            blocked_by=list(blocked_by or []),
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
            return f"{task_id} is {task.status}; cannot claim"
        if not self.can_start(task):
            return f"{task_id} is blocked by incomplete dependencies"
        task.status = "in_progress"
        task.owner = owner.strip() or "agent"
        self.save(task)
        return f"claimed {task.id}: {task.subject}"

    def complete(self, task_id: str) -> str:
        task = self.load(task_id)
        if task.status != "in_progress":
            return f"{task_id} is {task.status}; cannot complete"
        task.status = "completed"
        self.save(task)
        unblocked = [
            item.subject for item in self.list()
            if item.status == "pending" and item.blocked_by and self.can_start(item)
        ]
        suffix = f"\nunblocked: {', '.join(unblocked)}" if unblocked else ""
        return f"completed {task.id}: {task.subject}{suffix}"

    def render(self) -> str:
        tasks = self.list()
        if not tasks:
            return "no project tasks"
        icons = {"pending": " ", "in_progress": ">", "completed": "x"}
        lines = []
        for task in tasks:
            owner = f" owner={task.owner}" if task.owner else ""
            deps = f" blocked_by={','.join(task.blocked_by or [])}" if task.blocked_by else ""
            lines.append(f"[{icons[task.status]}] {task.id}: {task.subject} ({task.status}){owner}{deps}")
        return "\n".join(lines)

