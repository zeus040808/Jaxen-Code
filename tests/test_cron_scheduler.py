import json
from datetime import datetime

import pytest

from hh_agent import cron_scheduler


@pytest.fixture(autouse=True)
def isolated_scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(cron_scheduler, "DURABLE_PATH", tmp_path / ".scheduled_tasks.json")
    monkeypatch.setattr(cron_scheduler, "scheduled_jobs", {})
    monkeypatch.setattr(cron_scheduler, "cron_queue", [])
    monkeypatch.setattr(cron_scheduler, "_last_fired", {})
    monkeypatch.setattr(cron_scheduler, "_scheduler_started", False)


@pytest.mark.parametrize("expression,expected", [
    ("* * * * *", True),
    ("*/5 * * * *", True),
    ("11 9 * * *", False),
    ("10 9 * * 1-5", True),
    ("10 9 * * 0", False),
])
def test_cron_matches(expression, expected):
    monday = datetime(2026, 7, 6, 9, 10)
    assert cron_scheduler.cron_matches(expression, monday) is expected


def test_day_and_weekday_use_or_semantics():
    monday_the_sixth = datetime(2026, 7, 6, 9, 0)
    assert cron_scheduler.cron_matches("0 9 7 * 1", monday_the_sixth)


@pytest.mark.parametrize("expression", [
    "* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "* * * * 7", "a * * * *",
])
def test_validate_cron_rejects_invalid_expression(expression):
    assert cron_scheduler.validate_cron(expression)


def test_schedule_list_cancel_and_persist():
    job = cron_scheduler.schedule_job("*/5 * * * *", "检查测试", durable=True)
    assert isinstance(job, cron_scheduler.CronJob)
    assert cron_scheduler.list_jobs() == [job]
    assert json.loads(cron_scheduler.DURABLE_PATH.read_text(encoding="utf-8"))[0]["id"] == job.id

    assert "已取消" in cron_scheduler.cancel_job(job.id)
    assert cron_scheduler.list_jobs() == []


def test_poll_fires_only_once_per_minute():
    job = cron_scheduler.schedule_job("* * * * *", "检查状态", durable=False)
    now = datetime(2026, 7, 6, 9, 10, 1)

    assert cron_scheduler.poll_due_jobs(now) == [job]
    assert cron_scheduler.poll_due_jobs(now.replace(second=50)) == []
    assert cron_scheduler.consume_cron_queue() == [job]
    assert not cron_scheduler.has_cron_queue()


def test_one_shot_job_is_removed_after_firing():
    job = cron_scheduler.schedule_job(
        "* * * * *", "只执行一次", recurring=False, durable=True
    )

    assert cron_scheduler.poll_due_jobs(datetime(2026, 7, 6, 9, 10)) == [job]
    assert cron_scheduler.list_jobs() == []
    assert json.loads(cron_scheduler.DURABLE_PATH.read_text(encoding="utf-8")) == []


def test_load_skips_invalid_durable_jobs():
    cron_scheduler.DURABLE_PATH.write_text(json.dumps([
        {"id": "cron_good", "cron": "0 9 * * *", "prompt": "工作", "recurring": True, "durable": True},
        {"id": "cron_bad", "cron": "99 9 * * *", "prompt": "坏任务", "recurring": True, "durable": True},
    ]), encoding="utf-8")

    assert cron_scheduler.load_durable_jobs() == 1
    assert [job.id for job in cron_scheduler.list_jobs()] == ["cron_good"]
