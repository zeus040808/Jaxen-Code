import json

import pytest

from hh_agent import task_system


@pytest.fixture(autouse=True)
def isolated_tasks_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(task_system, "TASKS_DIR", tmp_path / ".tasks")


def test_create_and_load_task():
    task = task_system.create_task("设计数据库", "先定义数据表")

    saved = task_system.load_task(task.id)
    assert saved.subject == "设计数据库"
    assert saved.status == "pending"
    assert json.loads(task_system.get_task(task.id))["description"] == "先定义数据表"


def test_dependency_blocks_then_unlocks():
    schema = task_system.create_task("设计数据库")
    api = task_system.create_task("实现 API", blockedBy=[schema.id])

    assert not task_system.can_start(api.id)
    assert "阻塞" in task_system.claim_task(api.id)

    assert "已认领" in task_system.claim_task(schema.id, owner="main")
    completed = task_system.complete_task(schema.id)

    assert "实现 API" in completed
    assert task_system.can_start(api.id)
    assert task_system.load_task(schema.id).owner == "main"


def test_missing_dependency_stays_blocked():
    task = task_system.create_task("实现 API", blockedBy=["task_missing_0000"])

    assert not task_system.can_start(task.id)
    assert "task_missing_0000" in task_system.claim_task(task.id)


def test_status_transitions_are_enforced():
    task = task_system.create_task("写测试")

    assert "无法完成" in task_system.complete_task(task.id)
    task_system.claim_task(task.id)
    assert "无法认领" in task_system.claim_task(task.id)
    assert "已完成" in task_system.complete_task(task.id)


def test_list_and_format_tasks():
    first = task_system.create_task("任务一")
    task_system.create_task("任务二", blockedBy=[first.id])

    assert len(task_system.list_tasks()) == 2
    output = task_system.format_tasks()
    assert "任务一" in output
    assert "blockedBy=" in output


def test_rejects_unsafe_task_id():
    with pytest.raises(ValueError, match="非法任务 ID"):
        task_system.load_task("../../outside")
