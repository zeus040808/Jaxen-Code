"""Session and run artifact persistence."""

from __future__ import annotations

import json
from pathlib import Path

from .state import RunState
from .workspace import now


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.sessions_dir = root / ".hh_coder" / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def new(self) -> dict:
        return {
            "id": now().replace(":", "").replace(".", "-"),
            "created_at": now(),
            "history": [],
            "memory": {"notes": [], "file_summaries": {}},
        }

    def path(self, session_id: str) -> Path:
        safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
        return self.sessions_dir / f"{safe}.json"

    def save(self, session: dict) -> Path:
        path = self.path(session["id"])
        path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load(self, session_id: str) -> dict:
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))


class RunStore:
    def __init__(self, root: Path):
        self.root = root
        self.runs_dir = root / ".hh_coder" / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        path = self.runs_dir / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_state(self, state: RunState) -> None:
        (self.run_dir(state.run_id) / "task_state.json").write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def trace(self, state: RunState, event: str, payload: dict | None = None) -> None:
        record = {"event": event, "created_at": now(), **(payload or {})}
        with (self.run_dir(state.run_id) / "trace.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def report(self, state: RunState, payload: dict) -> None:
        (self.run_dir(state.run_id) / "report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

