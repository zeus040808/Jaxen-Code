"""子 agent 的预算与边界；改代码后的验证识别。"""

from pathlib import Path

import pytest

from hh_agent_pro.agent import CodingAgent
from hh_agent_pro.providers import FakeModelClient
from hh_agent_pro.run_store import RunStore
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.task_state import _check_kind
from hh_agent_pro.tools import DEFAULT_DELEGATE_STEPS, MAX_DELEGATE_STEPS
from hh_agent_pro.workspace import WorkspaceContext


def _build_agent(tmp_path, outputs):
    root = Path(tmp_path) / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text("A = 1\n", encoding="utf-8")
    return CodingAgent(
        model_client=FakeModelClient(list(outputs)),
        workspace=WorkspaceContext.build(root, repo_root_override=root),
        session_store=SessionStore(root / ".hh" / "sessions"),
        run_store=RunStore(root / ".hh" / "runs"),
        approval_policy="auto",
    )


# ---- 子 agent ---------------------------------------------------------------

def test_delegate_defaults_to_a_real_investigation_budget(tmp_path):
    agent = _build_agent(tmp_path, ["<final>child answer</final>"])
    captured = {}
    original = CodingAgent.__init__

    def spy(self, *args, **kwargs):
        captured.setdefault("max_steps", kwargs.get("max_steps"))
        original(self, *args, **kwargs)

    CodingAgent.__init__ = spy
    try:
        result = agent.execute_tool("delegate", {"task": "find where A is defined"})
    finally:
        CodingAgent.__init__ = original

    assert result.metadata["tool_status"] == "ok"
    # 旧默认 3 步，只够读两三个文件。
    assert DEFAULT_DELEGATE_STEPS == 20
    assert captured["max_steps"] == DEFAULT_DELEGATE_STEPS


def test_delegate_budget_is_capped(tmp_path):
    agent = _build_agent(tmp_path, [])

    result = agent.execute_tool("delegate", {"task": "x", "max_steps": MAX_DELEGATE_STEPS + 1})

    assert result.metadata["tool_error_code"] == "invalid_arguments"


def test_child_has_its_own_session_and_cannot_edit(tmp_path):
    agent = _build_agent(tmp_path, [
        '<tool>{"name":"write_file","args":{"path":"a.py","content":"A = 2\\n"}}</tool>',
        "<final>could not edit</final>",
    ])

    result = agent.execute_tool("delegate", {"task": "change A", "max_steps": 3})

    assert "<subagent-result>" in result.content and "<kind>delegate</kind>" in result.content
    assert (agent.root / "a.py").read_text(encoding="utf-8") == "A = 1\n"
    # 子 agent 的会话独立：主会话的历史里看不到它的工具调用。
    assert not any(item.get("name") == "write_file" for item in agent.session["history"])


# ---- 验证识别 -----------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "pytest -q",
    "python -m pytest tests/test_x.py",
    "cd /testbed && python -m pytest -q tests/test_x.py",
    'cd "my repo" && pytest',
])
def test_test_commands_are_recognized(command):
    assert _check_kind(command) == "test"


@pytest.mark.parametrize("command", ["pytest; true", "cd x && pytest || true", "pytest --collect-only"])
def test_commands_that_can_hide_failures_are_not_tests(command):
    assert _check_kind(command) != "test"
