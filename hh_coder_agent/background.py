"""Background tool execution notifications."""

from __future__ import annotations

import threading
from collections.abc import Callable

from .workspace import clip


SLOW_HINTS = ("pytest", "test", "build", "install", "compile", "npm install", "pip install")


class BackgroundManager:
    def __init__(self):
        self._counter = 0
        self._tasks: dict[str, dict] = {}
        self._results: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def should_background(tool_name: str, args: dict) -> bool:
        if bool(args.get("background")):
            return True
        if tool_name != "run_shell":
            return False
        command = str(args.get("command", "")).lower()
        return any(hint in command for hint in SLOW_HINTS)

    def start(self, command: str, runner: Callable[[], str]) -> str:
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self._tasks[task_id] = {"command": command, "status": "running"}

        def worker() -> None:
            status = "completed"
            try:
                result = runner()
            except Exception as error:
                status = "failed"
                result = f"background task failed: {error}"
            with self._lock:
                self._tasks[task_id]["status"] = status
                self._results[task_id] = str(result)

        threading.Thread(target=worker, daemon=True).start()
        return task_id

    def collect(self) -> list[str]:
        with self._lock:
            ready = [key for key, item in self._tasks.items()
                     if item["status"] in {"completed", "failed"}]
            finished = [
                (key, self._tasks.pop(key), self._results.pop(key, ""))
                for key in ready
            ]
        notes = []
        for task_id, task, output in finished:
            notes.append(
                "<task_notification>\n"
                f"<task_id>{task_id}</task_id>\n"
                f"<status>{task['status']}</status>\n"
                f"<command>{task['command']}</command>\n"
                f"<summary>{clip(output, 2000)}</summary>\n"
                "</task_notification>"
            )
        return notes

