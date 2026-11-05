"""P1-e：会话内轻量 todo 列表（对应 Claude Code 的 TodoWrite）。

长任务里模型容易做着做着忘了还剩哪几步。todo_write 每次整体替换列表，
列表渲染进每轮都会发送的 Goal progress，模型每一步都能看到完整计划。
"""

from pathlib import Path

from jaxen.providers import FakeModelClient
from jaxen.run_store import RunStore
from jaxen.agent import CodingAgent
from jaxen.session_store import SessionStore
from jaxen.workspace import WorkspaceContext


def _build_agent(tmp_path, scripted=("<final>done</final>",)):
    root = Path(tmp_path) / "repo"
    root.mkdir(parents=True, exist_ok=True)
    workspace = WorkspaceContext.build(root, repo_root_override=root)
    return CodingAgent(
        model_client=FakeModelClient(list(scripted)),
        workspace=workspace,
        session_store=SessionStore(root / ".jaxen" / "sessions"),
        run_store=RunStore(root / ".jaxen" / "runs"),
        approval_policy="auto",
    )


TODOS = [
    {"content": "Reproduce the bug", "status": "completed"},
    {"content": "Fix the parser", "status": "in_progress"},
    {"content": "Run the tests", "status": "pending"},
]


def test_todo_write_replaces_list_and_renders_marks(tmp_path):
    agent = _build_agent(tmp_path)

    result = agent.execute_tool("todo_write", {"todos": TODOS})

    assert result.metadata["tool_status"] == "ok"
    assert "[x] Reproduce the bug" in result.content
    assert "[>] Fix the parser" in result.content
    assert "[ ] Run the tests" in result.content
    agent.execute_tool("todo_write", {"todos": TODOS[:1]})
    assert [item["content"] for item in agent.progress.to_dict()["todos"]] == ["Reproduce the bug"]


def test_todos_appear_in_next_model_request(tmp_path):
    agent = _build_agent(tmp_path)
    agent.execute_tool("todo_write", {"todos": TODOS})

    request, _ = agent.context_manager.prepare("fix it")

    assert "[>] Fix the parser" in request.messages[-1]["content"]


def test_invalid_status_is_rejected(tmp_path):
    agent = _build_agent(tmp_path)

    result = agent.execute_tool("todo_write", {"todos": [{"content": "x", "status": "done"}]})

    assert result.metadata["tool_status"] != "ok"
    assert agent.progress.to_dict()["todos"] == []


def test_new_task_starts_with_empty_todos(tmp_path):
    agent = _build_agent(tmp_path, [
        '<tool>{"name":"todo_write","args":{"todos":[{"content":"step","status":"in_progress"}]}}</tool>',
        "<final>first done</final>",
        "<final>second done</final>",
    ])
    agent.ask("first task")
    assert agent.progress.to_dict()["todos"]

    agent.ask("an unrelated second task")

    assert agent.progress.to_dict()["todos"] == []
