"""后台命令：run_shell 带 background=true 时在后台线程里跑慢命令，跑完以 <job_notification> 通知 agent。

叫 job 不叫 job：它只是一条后台 shell 命令，和进度账本（progress.py）、长任务待办（backlog.py）都无关。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from ..workspace import clip


class BackgroundJobs:
    def __init__(self):
        self._counter = 0
        self._jobs: dict[str, dict] = {}
        self._results: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def should_run_background(name: str, args: dict) -> bool:
        # Verification commands must return their exit code to the current
        # Agent Loop. Background execution is therefore explicit only.
        return name == "run_shell" and bool(args.get("background", False))

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job["status"] == "running")

    def start(self, command: str, runner: Callable[[], str]) -> str:
        with self._lock:
            self._counter += 1
            job_id = f"bg_{self._counter:04d}"
            self._jobs[job_id] = {"command": command, "status": "running"}

        def worker() -> None:
            status = "completed"
            try:
                output = runner()
            except Exception as error:
                status = "failed"
                output = f"background job failed: {error}"
            with self._lock:
                self._jobs[job_id]["status"] = status
                self._results[job_id] = str(output)

        threading.Thread(target=worker, daemon=True).start()
        return job_id

    def collect(self) -> list[str]:
        with self._lock:
            ready = [
                job_id for job_id, job in self._jobs.items()
                if job["status"] in {"completed", "failed"}
            ]
            finished = [
                (job_id, self._jobs.pop(job_id), self._results.pop(job_id, ""))
                for job_id in ready
            ]
        notifications = []
        for job_id, job, output in finished:
            notifications.append(
                "<job_notification>\n"
                f"<job_id>{job_id}</job_id>\n"
                f"<status>{job['status']}</status>\n"
                f"<command>{job['command']}</command>\n"
                f"<summary>{clip(output, 2000)}</summary>\n"
                "</job_notification>"
            )
        return notifications
