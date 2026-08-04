"""Serializable run state for audit artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .workspace import now


@dataclass
class ToolStep:
    name: str
    args: dict
    status: str
    preview: str
    created_at: str = field(default_factory=now)


@dataclass
class RunState:
    run_id: str
    user_request: str
    status: str = "running"
    attempts: int = 0
    tool_steps: int = 0
    stop_reason: str = ""
    final_answer: str = ""
    started_at: str = field(default_factory=now)
    finished_at: str = ""
    tools: list[ToolStep] = field(default_factory=list)

    def record_attempt(self) -> None:
        self.attempts += 1

    def record_tool(self, name: str, args: dict, status: str, preview: str) -> None:
        self.tool_steps += 1
        self.tools.append(ToolStep(name=name, args=args, status=status, preview=preview))

    def finish(self, answer: str, reason: str = "final") -> None:
        self.status = "completed"
        self.stop_reason = reason
        self.final_answer = answer
        self.finished_at = now()

    def stop(self, answer: str, reason: str) -> None:
        self.status = "stopped"
        self.stop_reason = reason
        self.final_answer = answer
        self.finished_at = now()

    def to_dict(self) -> dict:
        return asdict(self)

