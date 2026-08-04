import threading

import pytest

from hh_agent import background_tasks
from hh_agent.tools import SUB_TOOLS, TOOLS, run_bash


@pytest.fixture(autouse=True)
def reset_background_state(monkeypatch):
    monkeypatch.setattr(background_tasks, "_counter", 0)
    monkeypatch.setattr(background_tasks, "_tasks", {})
    monkeypatch.setattr(background_tasks, "_results", {})


def test_explicit_background_request():
    assert background_tasks.should_run_background(
        "run_bash", {"command": "echo hello", "run_in_background": True}
    )


def test_slow_command_heuristic():
    assert background_tasks.should_run_background("run_bash", {"command": "pytest -q"})
    assert not background_tasks.should_run_background("run_bash", {"command": "git status"})
    assert not background_tasks.should_run_background("read_file", {"path": "README.md"})


def test_bash_schema_exposes_background_flag():
    bash_tool = next(tool for tool in TOOLS if tool["function"]["name"] == "run_bash")
    properties = bash_tool["function"]["parameters"]["properties"]
    assert properties["run_in_background"]["type"] == "boolean"
    assert "run_in_background" not in bash_tool["function"]["parameters"]["required"]

    sub_bash = next(tool for tool in SUB_TOOLS if tool["function"]["name"] == "run_bash")
    assert "run_in_background" not in sub_bash["function"]["parameters"]["properties"]


def test_run_bash_accepts_dispatch_only_flag():
    assert "s13" in run_bash("echo s13", run_in_background=False)


def test_background_task_returns_id_before_completion():
    release = threading.Event()
    started = threading.Event()

    def runner():
        started.set()
        release.wait(timeout=2)
        return "done"

    task_id = background_tasks.start_background_task("slow command", runner)

    assert task_id == "bg_0001"
    assert started.wait(timeout=1)
    assert background_tasks.collect_background_results() == []

    release.set()
    for _ in range(100):
        notifications = background_tasks.collect_background_results()
        if notifications:
            break
        threading.Event().wait(0.01)

    assert "<task_id>bg_0001</task_id>" in notifications[0]
    assert "<status>completed</status>" in notifications[0]
    assert "done" in notifications[0]


def test_failed_background_task_becomes_notification():
    finished = threading.Event()

    def runner():
        try:
            raise RuntimeError("boom")
        finally:
            finished.set()

    background_tasks.start_background_task("bad command", runner)
    assert finished.wait(timeout=1)

    notifications = background_tasks.collect_background_results()
    assert "<status>failed</status>" in notifications[0]
    assert "boom" in notifications[0]
