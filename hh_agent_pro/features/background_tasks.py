"""Background task runner for slow shell commands."""

from __future__ import annotations

import threading
from collections.abc import Callable

from ..workspace import clip


SLOW_COMMAND_HINTS = (
    "pytest", "test", "build", "install", "compile", "deploy",
    "npm install", "pip install", "cargo build", "docker build",
)


class BackgroundTaskManager:
    def __init__(self):
        self._counter = 0
        self._tasks: dict[str, dict] = {}
        self._results: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def should_run_background(name: str, args: dict) -> bool:
        # An explicit value must override the slow-command heuristic. The
        # background worker sets this to False so it executes exactly once.
        if "background" in args:
            return bool(args.get("background"))
        if name != "run_shell":
            return False
        command = str(args.get("command", "")).lower()
        return any(hint in command for hint in SLOW_COMMAND_HINTS)

    def start(self, command: str, runner: Callable[[], str]) -> str:
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self._tasks[task_id] = {"command": command, "status": "running"}

        def worker() -> None:
            status = "completed"
            try:
                output = runner()
            except Exception as error:
                status = "failed"
                output = f"background task failed: {error}"
            with self._lock:
                self._tasks[task_id]["status"] = status
                self._results[task_id] = str(output)

        threading.Thread(target=worker, daemon=True).start()
        return task_id

    def collect(self) -> list[str]:
        with self._lock:
            ready = [
                task_id for task_id, task in self._tasks.items()
                if task["status"] in {"completed", "failed"}
            ]
            finished = [
                (task_id, self._tasks.pop(task_id), self._results.pop(task_id, ""))
                for task_id in ready
            ]
        notifications = []
        for task_id, task, output in finished:
            notifications.append(
                "<task_notification>\n"
                f"<task_id>{task_id}</task_id>\n"
                f"<status>{task['status']}</status>\n"
                f"<command>{task['command']}</command>\n"
                f"<summary>{clip(output, 2000)}</summary>\n"
                "</task_notification>"
            )
        return notifications
