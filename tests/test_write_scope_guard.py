"""写入范围必须由运行时强制，而不是靠提示词请求模型自觉。

评测证据（2026-08-19）：
  * `robust_port_schema` 只允许改 `acme/validators.py`，模型顺手改了
    `tests/test_smoke.py`——等于自己改测试让测试通过。
  * `session_conflict_resolution` 三次运行改动的文件集合各不相同，
    只有 1/3 守住了范围。

结论：范围蔓延是跨套件的稳定倾向，提示词约束不住。write_scope 把它
升级成和「路径逃逸」同级的硬护栏。
"""

from pathlib import Path

import pytest

from hh_agent_pro.providers.clients import FakeModelClient
from hh_agent_pro.run_store import RunStore
from hh_agent_pro.runtime import HH, SessionStore
from hh_agent_pro.workspace import WorkspaceContext


def _build_agent(tmp_path, write_scope, scripted=None):
    root = Path(tmp_path) / "repo"
    (root / "acme").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "acme" / "validators.py").write_text("PLACEHOLDER = 1\n", encoding="utf-8")
    (root / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    workspace = WorkspaceContext.build(root, repo_root_override=root)
    return HH(
        model_client=FakeModelClient(list(scripted or ["<final>done</final>"])),
        workspace=workspace,
        session_store=SessionStore(root / ".hh" / "sessions"),
        run_store=RunStore(root / ".hh" / "runs"),
        approval_policy="auto",
        write_scope=write_scope,
        max_steps=4,
    )


def test_unrestricted_by_default(tmp_path):
    """不传 write_scope 时行为不变，老调用方不受影响。"""

    agent = _build_agent(tmp_path, None)

    assert agent.write_scope is None
    assert agent.write_scope_violations(["tests/test_smoke.py", "anything/else.py"]) == []


def test_empty_scope_is_rejected_rather_than_silently_open(tmp_path):
    """空列表最危险：看起来设了限制，实际什么都没限制。"""

    with pytest.raises(ValueError):
        _build_agent(tmp_path, [])


def test_write_outside_scope_is_rejected_before_it_lands(tmp_path):
    """核心用例：改测试文件必须在落盘前被拦下。"""

    agent = _build_agent(tmp_path, ["acme/validators.py"])
    original = (agent.root / "tests" / "test_smoke.py").read_text(encoding="utf-8")

    result = agent.execute_tool(
        "write_file",
        {"path": "tests/test_smoke.py", "content": "def test_ok():\n    assert True  # 放水\n"},
    )

    assert result.metadata["tool_status"] == "rejected"
    assert result.metadata["tool_error_code"] == "write_scope_violation"
    assert result.metadata["security_event_type"] == "write_scope_violation"
    assert result.metadata["retryable"] is False
    assert "tests/test_smoke.py" in result.content
    # 最要紧的一条：文件没被动过。
    assert (agent.root / "tests" / "test_smoke.py").read_text(encoding="utf-8") == original


def test_write_inside_scope_still_works(tmp_path):
    """护栏不能误伤正常写入。"""

    agent = _build_agent(tmp_path, ["acme/validators.py"])

    result = agent.execute_tool(
        "write_file",
        {"path": "acme/validators.py", "content": "def is_valid_port(value):\n    return True\n"},
    )

    assert result.metadata["tool_status"] == "ok"
    assert "is_valid_port" in (agent.root / "acme" / "validators.py").read_text(encoding="utf-8")


def test_glob_patterns_are_supported(tmp_path):
    """真实任务常给的是目录级范围，而不是单个文件。"""

    agent = _build_agent(tmp_path, ["acme/*.py"])

    assert agent.write_scope_violations(["acme/validators.py"]) == []
    assert agent.write_scope_violations(["tests/test_smoke.py"]) == ["tests/test_smoke.py"]


def test_patch_file_is_guarded_too(tmp_path):
    """patch_file 和 write_file 走同一条护栏。"""

    agent = _build_agent(tmp_path, ["acme/validators.py"])

    result = agent.execute_tool(
        "patch_file",
        {"path": "tests/test_smoke.py", "old_text": "assert True", "new_text": "pass"},
    )

    assert result.metadata["tool_error_code"] == "write_scope_violation"
    assert "assert True" in (agent.root / "tests" / "test_smoke.py").read_text(encoding="utf-8")


def test_shell_writes_outside_scope_are_reported_after_the_fact(tmp_path):
    """run_shell 事后才知道动了什么——拦不住，但必须报出来并停下。"""

    agent = _build_agent(tmp_path, ["acme/validators.py"])
    target = (agent.root / "tests" / "test_smoke.py").as_posix()

    result = agent.execute_tool(
        "run_shell",
        {"command": f'python -c "open(r\'{target}\',\'a\').write(\'# touched\\n\')"', "timeout": 30},
    )

    assert result.metadata["tool_error_code"] == "write_scope_violation"
    assert result.metadata["stop_run"] is True
    assert "tests/test_smoke.py" in result.metadata["out_of_scope_paths"]
