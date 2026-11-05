"""Background task runner for slow shell commands."""

from __future__ import annotations

import threading
from collections.abc import Callable

from ..workspace import clip


class BackgroundTaskManager:
    def __init__(self):
        self._counter = 0
        self._tasks: dict[str, dict] = {}
        self._results: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def should_run_background(name: str, args: dict) -> bool:
        # Verification commands must return their exit code to the current
        # Agent Loop. Background execution is therefore explicit only.
        return name == "run_shell" and bool(args.get("background", False))

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for task in self._tasks.values() if task["status"] == "running")

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
