"""P1-d：core 工具集只暴露编码工具，系统提示只讲已暴露工具的规则。

原先每轮把 26 个工具和约 37 条规则全部发给模型，其中 task/cron/memory/
skill/session 相关的十几个和写代码无关，分散弱模型的注意力。
"""

from pathlib import Path

import pytest

from jaxen.cli import build_arg_parser
from jaxen.providers import FakeModelClient
from jaxen.run_store import RunStore
from jaxen.agent import CodingAgent
from jaxen.session_store import SessionStore
from jaxen.workspace import WorkspaceContext

CORE = {
    "list_files", "read_file", "read_files", "search", "glob", "repo_map", "runtime_info",
    "run_shell", "write_file", "patch_file", "todo_write", "investigate",
}


def _build_agent(tmp_path, **kwargs):
    root = Path(tmp_path) / "repo"
    root.mkdir(parents=True, exist_ok=True)
    workspace = WorkspaceContext.build(root, repo_root_override=root)
    return CodingAgent(
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=SessionStore(root / ".jaxen" / "sessions"),
        run_store=RunStore(root / ".jaxen" / "runs"),
        approval_policy="auto",
        **kwargs,
    )


def test_core_toolset_exposes_only_coding_tools(tmp_path):
    agent = _build_agent(tmp_path, toolset="core")

    assert set(agent.tools) == CORE


def test_full_toolset_keeps_extended_tools_for_api_callers(tmp_path):
    agent = _build_agent(tmp_path)

    assert agent.toolset == "full"
    assert CORE <= set(agent.tools)
    assert {"memory_update", "backlog_add", "record_claim", "skill_view", "session_search"} <= set(agent.tools)


def test_core_prompt_omits_rules_for_missing_tools(tmp_path):
    core = _build_agent(tmp_path, toolset="core").prefix_state.text
    full = _build_agent(tmp_path / "full", toolset="full").prefix_state.text

    assert "Read a file before editing it" in core
    assert "Do not add, modify, or delete test files unless the user asks" in core
    assert "todo_write" in core
    assert "Durable memory is curated" not in core
    assert "skill_view" not in core
    assert "Durable memory is curated" in full


def test_cli_defaults_to_core_toolset():
    assert build_arg_parser().parse_args([]).toolset == "core"


def test_unknown_toolset_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _build_agent(tmp_path, toolset="everything")
