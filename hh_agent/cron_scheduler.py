"""S14 cron scheduler: persistent schedules, polling thread, and trigger queue."""
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


DURABLE_PATH = Path.cwd() / ".scheduled_tasks.json"


@dataclass
class CronJob:
    id: str
    cron: str  #五段式时间表达式
    prompt: str
    recurring: bool #是否周期执行
    durable: bool #是否写入磁盘


scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
_last_fired: dict[str, str] = {}
_lock = threading.Lock()
_scheduler_started = False

# 单字段匹配
def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value) for part in field.split(","))
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)

# 完整时间匹配
def cron_matches(cron_expr: str, when: datetime) -> bool:
    """Match a five-field cron expression using standard DOM/DOW OR semantics."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    # 星期转换
    cron_weekday = (when.weekday() + 1) % 7  # Python Monday=0; cron Sunday=0.

    if not (_cron_field_matches(minute, when.minute)
            and _cron_field_matches(hour, when.hour)
            and _cron_field_matches(month, when.month)):
        return False

    day_matches = _cron_field_matches(day, when.day)
    weekday_matches = _cron_field_matches(weekday, cron_weekday)
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    return day_matches or weekday_matches


def _validate_field(field: str, minimum: int, maximum: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"非法步长: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            error = _validate_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    if "-" in field:
        parts = field.split("-", 1)
        if not all(part.isdigit() for part in parts):
            return f"非法范围: {field}"
        start, end = map(int, parts)
        if not minimum <= start <= end <= maximum:
            return f"范围越界: {field}，应在 {minimum}-{maximum}"
        return None
    if not field.isdigit():
        return f"非法字段: {field}"
    if not minimum <= int(field) <= maximum:
        return f"数值越界: {field}，应在 {minimum}-{maximum}"
    return None

# 表达式校验
def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"cron 表达式应有 5 段，实际为 {len(fields)} 段"
    specs = [
        ("分钟", 0, 59), ("小时", 0, 23), ("日期", 1, 31),
        ("月份", 1, 12), ("星期", 0, 6),
    ]
    for field, (name, minimum, maximum) in zip(fields, specs):
        error = _validate_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"
    return None

# 持久化保存job
def _save_durable_jobs() -> None:
    durable_jobs = [asdict(job) for job in scheduled_jobs.values() if job.durable]
    DURABLE_PATH.write_text(
        json.dumps(durable_jobs, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_durable_jobs() -> int:
    if not DURABLE_PATH.exists():
        return 0
    try:
        data = json.loads(DURABLE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"[cron] 读取持久化任务失败: {error}")
        return 0

    loaded = 0
    with _lock:
        for item in data:
            try:
                job = CronJob(**item)
            except (TypeError, KeyError):
                continue
            if validate_cron(job.cron) or not job.prompt.strip():
                continue
            scheduled_jobs[job.id] = job
            loaded += 1
    return loaded

# 创建定时任务
def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    # 检查 cron 表达式是否合法
    error = validate_cron(cron)
    if error:
        return error
    if not prompt.strip():
        return "定时任务内容不能为空"
    job = CronJob(
        id=f"cron_{secrets.token_hex(3)}",
        cron=cron.strip(),
        prompt=prompt.strip(),
        recurring=recurring,
        durable=durable,
    )
    with _lock:
        scheduled_jobs[job.id] = job
        if durable:
            _save_durable_jobs()
    return job

# 查看定时任务
def list_jobs() -> list[CronJob]:
    with _lock:
        return list(scheduled_jobs.values())

# 取消定时任务
def cancel_job(job_id: str) -> str:
    with _lock:
        job = scheduled_jobs.pop(job_id, None)
        _last_fired.pop(job_id, None)
        if job and job.durable:
            _save_durable_jobs()
    return f"已取消 {job_id}" if job else f"未找到定时任务 {job_id}"

# 检查和触发任务
def poll_due_jobs(when: datetime | None = None) -> list[CronJob]:
    """Check one instant, enqueue newly due jobs, and return what fired."""
    when = when or datetime.now()
    minute_marker = when.strftime("%Y-%m-%d %H:%M")
    fired: list[CronJob] = []
    durable_changed = False
    with _lock:
        for job in list(scheduled_jobs.values()):
            try:
                # 如果 cron_matches(job.cron, when) 为真，说明当前时间命中了这个任务
                if not cron_matches(job.cron, when):
                    continue
                if _last_fired.get(job.id) == minute_marker:
                    continue
                cron_queue.append(job)
                fired.append(job)
                _last_fired[job.id] = minute_marker
                if not job.recurring:
                    scheduled_jobs.pop(job.id, None)
                    durable_changed = durable_changed or job.durable
            except (ValueError, TypeError) as error:
                print(f"[cron] {job.id} 检查失败: {error}")
        if durable_changed:
            _save_durable_jobs()
    return fired


def consume_cron_queue() -> list[CronJob]:
    with _lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


def has_cron_queue() -> bool:
    with _lock:
        return bool(cron_queue)

# 调度线程内部代码
def _scheduler_loop() -> None:
    while True:
        time.sleep(1)
        poll_due_jobs()


def start_scheduler() -> None:
    """Load durable jobs once and start the daemon polling thread once."""
    global _scheduler_started
    with _lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    loaded = load_durable_jobs()
    # 进入线程
    # daemon=True 表示 Agent 进程退出时，调度线程一起结束
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print(f"[cron] 调度器已启动，恢复 {loaded} 个持久化任务")


def format_jobs() -> str:
    jobs = list_jobs()
    if not jobs:
        return "暂无定时任务"
    lines = []
    for job in jobs:
        mode = "周期" if job.recurring else "单次"
        storage = "持久化" if job.durable else "会话"
        lines.append(f"{job.id}: '{job.cron}' -> {job.prompt} [{mode}, {storage}]")
    return "\n".join(lines)
