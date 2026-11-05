"""定时提示：用户在 CLI 里用 /schedule 安排"到点自动发给 agent 的一条指令"。

这是用户功能，不是模型工具：agent 不知道定时器的存在，到点时 CLI 直接把这条指令当成用户消息调用 agent.ask()。
用 5 段 cron 表达式（分 时 日 月 周），持久化在 .jaxen/schedules.json。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class ScheduledPrompt:
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
        if not start.isdigit() or not end.isdigit():
            return f"invalid range: {field}"
        start_int, end_int = int(start), int(end)
        if not minimum <= start_int <= end_int <= maximum:
            return f"range out of bounds: {field}"
        return None
    if not field.isdigit() or not minimum <= int(field) <= maximum:
        return f"field out of bounds: {field}"
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


class PromptScheduler:
    def __init__(self, root: Path):
        self.path = Path(root) / ".jaxen" / "schedules.json"
        self.jobs: dict[str, ScheduledPrompt] = {}
        self.queue: list[ScheduledPrompt] = []
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
                    job = ScheduledPrompt(**item)
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

    def schedule(self, cron: str, prompt: str, recurring: bool = True, durable: bool = True) -> ScheduledPrompt | str:
        error = validate_cron(cron)
        if error:
            return error
        if not prompt.strip():
            return "prompt must not be empty"
        job = ScheduledPrompt(f"sched_{secrets.token_hex(3)}", cron.strip(), prompt.strip(), recurring, durable)
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
        return f"cancelled {job_id}" if job else f"scheduled prompt not found: {job_id}"

    def render(self) -> str:
        with self.lock:
            jobs = list(self.jobs.values())
        if not jobs:
            return "no scheduled prompts"
        lines = []
        for job in jobs:
            mode = "recurring" if job.recurring else "once"
            storage = "durable" if job.durable else "session"
            lines.append(f"{job.id}: '{job.cron}' -> {job.prompt} [{mode}, {storage}]")
        return "\n".join(lines)

    def poll(self, when: datetime | None = None) -> list[ScheduledPrompt]:
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

    def pop_due(self) -> ScheduledPrompt | None:
        """取出一条到点的提示（CLI 逐条交给 agent.ask）。"""
        with self.lock:
            return self.queue.pop(0) if self.queue else None

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

