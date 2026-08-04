"""Small cron scheduler for wake-up style agent tasks."""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool = True
    durable: bool = True


def _field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_field_matches(part.strip(), value) for part in field.split(","))
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(expr: str, when: datetime) -> bool:
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    cron_weekday = (when.weekday() + 1) % 7
    if not (_field_matches(minute, when.minute)
            and _field_matches(hour, when.hour)
            and _field_matches(month, when.month)):
        return False
    day_ok = _field_matches(day, when.day)
    weekday_ok = _field_matches(weekday, cron_weekday)
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_ok
    if weekday == "*":
        return day_ok
    return day_ok or weekday_ok


def _validate_field(field: str, minimum: int, maximum: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            error = _validate_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit() or not minimum <= int(start) <= int(end) <= maximum:
            return f"invalid range: {field}"
        return None
    if not field.isdigit() or not minimum <= int(field) <= maximum:
        return f"out of bounds: {field}"
    return None


def validate_cron(expr: str) -> str | None:
    fields = expr.strip().split()
    if len(fields) != 5:
        return f"cron expression must have 5 fields, got {len(fields)}"
    specs = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    for field, (minimum, maximum) in zip(fields, specs):
        error = _validate_field(field, minimum, maximum)
        if error:
            return error
    return None


class CronScheduler:
    def __init__(self, root: Path):
        self.path = root / ".hh_coder" / "scheduled_tasks.json"
        self.jobs: dict[str, CronJob] = {}
        self.queue: list[CronJob] = []
        self.last_fired: dict[str, str] = {}
        self.lock = threading.Lock()
        self.started = False

    def load(self) -> int:
        if not self.path.exists():
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        loaded = 0
        with self.lock:
            for item in data:
                try:
                    job = CronJob(**item)
                except TypeError:
                    continue
                if not validate_cron(job.cron) and job.prompt.strip():
                    self.jobs[job.id] = job
                    loaded += 1
        return loaded

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        durable = [asdict(job) for job in self.jobs.values() if job.durable]
        self.path.write_text(json.dumps(durable, ensure_ascii=False, indent=2), encoding="utf-8")

    def schedule(self, cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> CronJob | str:
        error = validate_cron(cron)
        if error:
            return error
        if not prompt.strip():
            return "prompt cannot be empty"
        job = CronJob(f"cron_{secrets.token_hex(3)}", cron.strip(), prompt.strip(), recurring, durable)
        with self.lock:
            self.jobs[job.id] = job
            if durable:
                self.save()
        return job

    def cancel(self, job_id: str) -> str:
        with self.lock:
            job = self.jobs.pop(job_id, None)
            self.last_fired.pop(job_id, None)
            if job and job.durable:
                self.save()
        return f"cancelled {job_id}" if job else f"cron job not found: {job_id}"

    def list_text(self) -> str:
        with self.lock:
            jobs = list(self.jobs.values())
        if not jobs:
            return "no cron jobs"
        return "\n".join(
            f"{job.id}: {job.cron} -> {job.prompt} "
            f"[{'recurring' if job.recurring else 'once'}, {'durable' if job.durable else 'session'}]"
            for job in jobs
        )

    def poll(self, when: datetime | None = None) -> list[CronJob]:
        when = when or datetime.now()
        marker = when.strftime("%Y-%m-%d %H:%M")
        fired = []
        changed = False
        with self.lock:
            for job in list(self.jobs.values()):
                if not cron_matches(job.cron, when):
                    continue
                if self.last_fired.get(job.id) == marker:
                    continue
                self.queue.append(job)
                fired.append(job)
                self.last_fired[job.id] = marker
                if not job.recurring:
                    self.jobs.pop(job.id, None)
                    changed = changed or job.durable
            if changed:
                self.save()
        return fired

    def consume(self) -> list[CronJob]:
        with self.lock:
            jobs = list(self.queue)
            self.queue.clear()
        return jobs

    def has_queue(self) -> bool:
        with self.lock:
            return bool(self.queue)

    def start(self) -> int:
        with self.lock:
            if self.started:
                return 0
            self.started = True
        loaded = self.load()

        def loop() -> None:
            while True:
                time.sleep(1)
                self.poll()

        threading.Thread(target=loop, daemon=True).start()
        return loaded

