"""P1-a：编辑类工具的四条行为。

1. write_file 覆盖已有文件前必须先读；patch_file 靠 old_text 唯一匹配自证，
   不强制先读；任何编辑在读后文件又被改时都拒绝。新建文件不受限。
2. patch_file / write_file 返回 diff，模型不用再读一遍确认。
3. read_file 默认读 2000 行，超长时按整行截断并提示从哪一行继续。
4. glob 按模式找文件。
"""

from pathlib import Path

from hh_agent_pro.providers import FakeModelClient
from hh_agent_pro.run_store import RunStore
from hh_agent_pro.agent import CodingAgent
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.workspace import WorkspaceContext


def _build_agent(tmp_path):
    root = Path(tmp_path) / "repo"
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "a.py").write_text("value = 1\nother = 2\n", encoding="utf-8")
    (root / "pkg" / "b.txt").write_text("text\n", encoding="utf-8")
    workspace = WorkspaceContext.build(root, repo_root_override=root)
    return CodingAgent(
        model_client=FakeModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=SessionStore(root / ".hh" / "sessions"),
        run_store=RunStore(root / ".hh" / "runs"),
        approval_policy="auto",
    )


def _patch(agent, old, new):
    return agent.execute_tool("patch_file", {"path": "pkg/a.py", "old_text": old, "new_text": new})


def test_overwrite_without_reading_is_refused_and_file_untouched(tmp_path):
    agent = _build_agent(tmp_path)

    result = agent.execute_tool("write_file", {"path": "pkg/a.py", "content": "value = 9\n"})

    assert result.metadata["tool_error_code"] == "file_not_read"
    assert result.metadata["retryable"] is True
    assert "value = 1" in (agent.root / "pkg" / "a.py").read_text(encoding="utf-8")


def test_patch_without_reading_is_allowed(tmp_path):
    # old_text 必须唯一匹配，已经证明模型知道文件内容。
    agent = _build_agent(tmp_path)

    assert _patch(agent, "value = 1", "value = 9").metadata["tool_status"] == "ok"


def test_edit_after_read_succeeds_and_returns_diff(tmp_path):
    agent = _build_agent(tmp_path)
    agent.execute_tool("read_file", {"path": "pkg/a.py"})

    result = _patch(agent, "value = 1", "value = 9")

    assert result.metadata["tool_status"] == "ok"
    assert "-value = 1" in result.content
    assert "+value = 9" in result.content


def test_consecutive_edits_do_not_need_rereading(tmp_path):
    agent = _build_agent(tmp_path)
    agent.execute_tool("read_file", {"path": "pkg/a.py"})
    _patch(agent, "value = 1", "value = 9")

    # 自己刚写入的内容算“已看过”。
    assert _patch(agent, "other = 2", "other = 3").metadata["tool_status"] == "ok"


def test_external_change_after_read_is_refused(tmp_path):
    agent = _build_agent(tmp_path)
    agent.execute_tool("read_file", {"path": "pkg/a.py"})
    (agent.root / "pkg" / "a.py").write_text("value = 1\nchanged = True\n", encoding="utf-8")

    result = _patch(agent, "value = 1", "value = 9")

    assert result.metadata["tool_error_code"] == "file_changed_since_read"


def test_read_files_counts_as_read(tmp_path):
    agent = _build_agent(tmp_path)
    agent.execute_tool("read_files", {"paths": ["pkg/a.py", "pkg/b.txt"]})

    result = agent.execute_tool("write_file", {"path": "pkg/b.txt", "content": "new\n"})

    assert result.metadata["tool_status"] == "ok"


def test_creating_new_file_needs_no_read(tmp_path):
    agent = _build_agent(tmp_path)

    result = agent.execute_tool("write_file", {"path": "pkg/new.py", "content": "x = 1\n"})

    assert result.metadata["tool_status"] == "ok"
    assert result.content.startswith("created pkg/new.py")


def test_overwrite_existing_file_returns_diff(tmp_path):
    agent = _build_agent(tmp_path)
    agent.execute_tool("read_file", {"path": "pkg/a.py"})

    result = agent.execute_tool("write_file", {"path": "pkg/a.py", "content": "value = 5\nother = 2\n"})

    assert result.metadata["tool_status"] == "ok"
    assert "+value = 5" in result.content


def test_read_file_defaults_to_2000_lines(tmp_path):
    agent = _build_agent(tmp_path)
    (agent.root / "pkg" / "short.py").write_text("\n".join(f"x{i}" for i in range(500)), encoding="utf-8")

    content = agent.execute_tool("read_file", {"path": "pkg/short.py"}).content

    # 500 行很短，旧默认 200 行会截在第 200 行。
    assert " 500: x499" in content
    assert "showing lines" not in content


def test_long_read_is_cut_at_a_line_with_continue_hint(tmp_path):
    agent = _build_agent(tmp_path)
    lines = [f"line_{i} " + "y" * 60 for i in range(1, 1501)]
    (agent.root / "pkg" / "long.py").write_text("\n".join(lines), encoding="utf-8")

    content = agent.execute_tool("read_file", {"path": "pkg/long.py"}).content

    assert "omitted" not in content  # 代码不走头尾截断
    assert "of 1500; call read_file with start=" in content
    last_shown = content.rsplit("\n", 2)[-2]
    assert last_shown.split(":", 1)[1].strip().startswith("line_")


def test_glob_finds_files_by_pattern(tmp_path):
    agent = _build_agent(tmp_path)

    content = agent.execute_tool("glob", {"pattern": "**/*.py"}).content

    assert "pkg/a.py" in content.replace("\\", "/")
    assert "b.txt" not in content
