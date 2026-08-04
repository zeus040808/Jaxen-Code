"""Persistent project task graph."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path


STATUSES = {"pending", "in_progress", "completed"}


@dataclass
class ProjectTask:
    id: str
    subject: str
    description: str
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] | None = None


class TaskStore:
    def __init__(self, root: Path):
        self.dir = root / ".hh_coder" / "tasks"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        if not task_id.startswith("task_") or not task_id.replace("_", "").isalnum():
            raise ValueError(f"invalid task id: {task_id}")
        return self.dir / f"{task_id}.json"

    def save(self, task: ProjectTask) -> None:
        if task.status not in STATUSES:
            raise ValueError(f"invalid task status: {task.status}")
        self._path(task.id).write_text(json.dumps(asdict(task), ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, task_id: str) -> ProjectTask:
        return ProjectTask(**json.loads(self._path(task_id).read_text(encoding="utf-8")))

    def create(self, subject: str, description: str = "", blocked_by: list[str] | None = None) -> ProjectTask:
        if not subject.strip():
            raise ValueError("task subject cannot be empty")
        for dep in blocked_by or []:
            self._path(dep)
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
        for dep_id in task.blocked_by or []:
            dep_path = self._path(dep_id)
            if not dep_path.exists() or self.load(dep_id).status != "completed":
                return False
        return True

    def claim(self, task_id: str, owner: str = "agent") -> str:
        task = self.load(task_id)
        if task.status != "pending":
            return f"{task_id} is {task.status}, cannot claim"
        if not self.can_start(task):
            return f"{task_id} is blocked by incomplete dependencies"
        task.status = "in_progress"
        task.owner = owner
        self.save(task)
        return f"claimed {task.id}: {task.subject}"

    def complete(self, task_id: str) -> str:
        task = self.load(task_id)
        if task.status != "in_progress":
            return f"{task_id} is {task.status}, cannot complete"
        task.status = "completed"
        self.save(task)
        unblocked = [item.subject for item in self.list()
                     if item.status == "pending" and item.blocked_by and self.can_start(item)]
        suffix = f"\nunblocked: {', '.join(unblocked)}" if unblocked else ""
        return f"completed {task.id}: {task.subject}{suffix}"

    def format(self) -> str:
        tasks = self.list()
        if not tasks:
            return "no project tasks"
        icons = {"pending": " ", "in_progress": ">", "completed": "x"}
        lines = []
        for task in tasks:
            deps = f" blocked_by={','.join(task.blocked_by or [])}" if task.blocked_by else ""
            owner = f" owner={task.owner}" if task.owner else ""
            lines.append(f"[{icons[task.status]}] {task.id}: {task.subject} ({task.status}){owner}{deps}")
        return "\n".join(lines)

