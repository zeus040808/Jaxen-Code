"""跨会话的待办清单（长任务）。

每一项带依赖、验收标准和证据，存在 .jaxen/backlog/，会话结束也不丢；只在 full 工具集里暴露给模型。
当前请求内的计划用 todo（progress.py），两者互不依赖：待办没做完不会拦当前请求的交付。
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path


VALID_STATUSES = {"pending", "in_progress", "completed", "failed", "blocked", "cancelled"}


@dataclass
class BacklogItem:
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


class Backlog:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / ".jaxen" / "backlog"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, item_id: str) -> Path:
        if not item_id.startswith("item_") or not item_id.replace("_", "").isalnum():
            raise ValueError(f"invalid backlog item id: {item_id}")
        return self.dir / f"{item_id}.json"

    def save(self, item: BacklogItem) -> None:
        if item.status not in VALID_STATUSES:
            raise ValueError(f"invalid backlog item status: {item.status}")
        self._path(item.id).write_text(
            json.dumps(asdict(item), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, item_id: str) -> BacklogItem:
        item = BacklogItem(**json.loads(self._path(item_id).read_text(encoding="utf-8")))
        if item.status not in VALID_STATUSES:
            raise ValueError(f"invalid backlog item status: {item.status}")
        return item

    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> BacklogItem:
        if not subject.strip():
            raise ValueError("subject must not be empty")
        for dependency in blocked_by or []:
            self._path(dependency)
        item = BacklogItem(
            id=f"item_{int(time.time())}_{secrets.token_hex(2)}",
            subject=subject.strip(),
            description=description.strip(),
            blocked_by=list(blocked_by or []),
            acceptance_criteria=[str(item).strip() for item in acceptance_criteria or [] if str(item).strip()],
            evidence=[],
            updated_at=str(time.time()),
        )
        self.save(item)
        return item

    def list(self) -> list[BacklogItem]:
        return [self.load(path.stem) for path in sorted(self.dir.glob("item_*.json"))]

    def can_start(self, item: BacklogItem) -> bool:
        for dependency in item.blocked_by or []:
            path = self._path(dependency)
            if not path.exists() or self.load(dependency).status != "completed":
                return False
        return True

    def claim(self, item_id: str, owner: str = "agent") -> str:
        item = self.load(item_id)
        if item.status != "pending":
            return f"error: {item_id} is {item.status}; cannot claim"
        if not self.can_start(item):
            return f"error: {item_id} is blocked by incomplete dependencies"
        item.status = "in_progress"
        item.owner = owner.strip() or "agent"
        item.updated_at = str(time.time())
        self.save(item)
        return f"claimed {item.id}: {item.subject}"

    def complete(self, item_id: str, evidence: list[str] | None = None) -> str:
        item = self.load(item_id)
        if item.status != "in_progress":
            return f"error: {item_id} is {item.status}; cannot complete"
        item.evidence = list(item.evidence or [])
        item.evidence.extend(str(item).strip() for item in evidence or [] if str(item).strip())
        required = len(item.acceptance_criteria or [])
        if required and len(item.evidence) < required:
            return f"error: {item_id} needs at least {required} evidence item(s) before completion"
        item.status = "completed"
        item.failure_reason = ""
        item.updated_at = str(time.time())
        self.save(item)
        unblocked = [
            item.subject for item in self.list()
            if item.status == "pending" and item.blocked_by and self.can_start(item)
        ]
        suffix = f"\nunblocked: {', '.join(unblocked)}" if unblocked else ""
        return f"completed {item.id}: {item.subject}{suffix}"

    def fail(self, item_id: str, reason: str, evidence: list[str] | None = None) -> str:
        item = self.load(item_id)
        if item.status not in {"pending", "in_progress"}:
            return f"error: {item_id} is {item.status}; cannot fail"
        item.status = "failed"
        item.failure_reason = reason.strip()
        item.evidence = list(item.evidence or []) + [
            str(item).strip() for item in evidence or [] if str(item).strip()
        ]
        item.updated_at = str(time.time())
        self.save(item)
        return f"failed {item.id}: {item.failure_reason}"

    def block(self, item_id: str, reason: str) -> str:
        item = self.load(item_id)
        if item.status not in {"pending", "in_progress"}:
            return f"error: {item_id} is {item.status}; cannot block"
        item.status = "blocked"
        item.failure_reason = reason.strip()
        item.updated_at = str(time.time())
        self.save(item)
        return f"blocked {item.id}: {item.failure_reason}"

    def add_evidence(self, item_id: str, evidence: str) -> str:
        item = self.load(item_id)
        item.evidence = list(item.evidence or [])
        text = evidence.strip()
        if text and text not in item.evidence:
            item.evidence.append(text)
        item.updated_at = str(time.time())
        self.save(item)
        return f"recorded evidence for {item.id}"

    def render(self) -> str:
        items = self.list()
        if not items:
            return "no project items"
        icons = {"pending": " ", "in_progress": ">", "completed": "x", "failed": "!", "blocked": "#", "cancelled": "-"}
        lines = []
        for item in items:
            owner = f" owner={item.owner}" if item.owner else ""
            deps = f" blocked_by={','.join(item.blocked_by or [])}" if item.blocked_by else ""
            criteria = f" criteria={len(item.acceptance_criteria or [])}" if item.acceptance_criteria else ""
            evidence = f" evidence={len(item.evidence or [])}" if item.evidence else ""
            reason = f" reason={item.failure_reason}" if item.failure_reason else ""
            lines.append(f"[{icons[item.status]}] {item.id}: {item.subject} ({item.status}){owner}{deps}{criteria}{evidence}{reason}")
        return "\n".join(lines)
