"""代码定位工具：search（正则 + 过滤 + 输出模式）、glob、repo_map（结构与定义）。

问题：SWE-bench 镜像里没有 rg，agent 实际用的是"不区分大小写的子串匹配"，不支持正则、不能按文件过滤、
没有上下文行；也没有办法不读整个文件就知道函数在哪里。大仓库、中等题最容易卡在定位上。
"""

import subprocess
import types
from pathlib import Path

import pytest

from jaxen.tools import _SYMBOL_CACHE, tool_glob, tool_repo_map, tool_search, validate_tool


def _context(root):
    root = Path(root).resolve()
    return types.SimpleNamespace(root=root, path=lambda raw: (root / str(raw)).resolve())


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "filters.py").write_text(
        "import math\n\n\ndef floatformat(text, arg=-1):\n    value = float(text)\n    return round(value, arg)\n\n\n"
        "class Formatter(Base):\n    def format(self, value):\n        return floatformat(value)\n\n"
        "    async def aformat(self, value):\n        return value\n",
        encoding="utf-8",
    )
    (root / "pkg" / "sub" / "deep.py").write_text("def FloatFormat():\n    pass\n", encoding="utf-8")
    (root / "README.md").write_text("floatformat docs\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "lib.py").write_text("def floatformat():\n    pass\n", encoding="utf-8")
    (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


# ---- search ---------------------------------------------------------------

def test_search_supports_regex_and_smart_case(repo):
    ctx = _context(repo)

    lowercase = tool_search(ctx, {"pattern": r"def floatformat\("})
    mixed_case = tool_search(ctx, {"pattern": "FloatFormat"})

    assert "pkg/filters.py:4:def floatformat(text, arg=-1):" in lowercase
    assert "pkg/sub/deep.py" in lowercase  # 全小写的模式不区分大小写
    assert "pkg/sub/deep.py" in mixed_case and "pkg/filters.py" not in mixed_case


def test_search_respects_gitignore(repo):
    assert "node_modules" not in tool_search(_context(repo), {"pattern": "floatformat", "output_mode": "files"})


def test_search_glob_and_repo_map_skip_the_agent_state_dir_in_git_repos(repo):
    # 目标仓库的 .gitignore 不认识 .jaxen：git ls-files 会把 agent 自己的运行记录当未跟踪文件列出来。
    # 2026-11-05 在 SWE-bench transcript 里发现 search 的结果是 .jaxen/runs/.../run_state.json。
    (repo / ".jaxen" / "runs" / "r1").mkdir(parents=True)
    (repo / ".jaxen" / "runs" / "r1" / "run_state.json").write_text('{"note": "floatformat"}', encoding="utf-8")
    (repo / ".jaxen" / "runs" / "r1" / "notes.py").write_text("def floatformat():\n    pass\n", encoding="utf-8")
    ctx = _context(repo)

    assert ".jaxen" not in tool_search(ctx, {"pattern": "floatformat", "output_mode": "files"})
    assert ".jaxen" not in tool_glob(ctx, {"pattern": "**/*.py"})
    assert ".jaxen" not in tool_repo_map(ctx, {"pattern": "floatformat"})


def test_search_glob_files_and_count_modes(repo):
    ctx = _context(repo)

    files = tool_search(ctx, {"pattern": "floatformat", "glob": "*.py", "output_mode": "files"}).splitlines()
    counts = tool_search(ctx, {"pattern": "floatformat", "glob": "*.py", "output_mode": "count"})

    assert files == ["pkg/filters.py", "pkg/sub/deep.py"]  # README.md 被 glob 过滤掉
    assert "pkg/filters.py:2" in counts


def test_search_context_lines_use_grep_markers(repo):
    output = tool_search(_context(repo), {"pattern": "value = float", "context": 1})

    assert "pkg/filters.py-4-def floatformat(text, arg=-1):" in output
    assert "pkg/filters.py:5:    value = float(text)" in output
    assert "pkg/filters.py-6-    return round(value, arg)" in output


def test_search_truncation_says_how_to_narrow(repo):
    output = tool_search(_context(repo), {"pattern": "a", "max_results": 2})

    assert len([line for line in output.splitlines() if not line.startswith("...")]) == 2
    assert "more results" in output and "output_mode='files'" in output


def test_search_falls_back_outside_git(tmp_path):
    (tmp_path / "a.py").write_text("needle = 1\n", encoding="utf-8")

    assert "a.py:1:needle = 1" in tool_search(_context(tmp_path), {"pattern": "needle"})


@pytest.mark.parametrize("args", [
    {"pattern": "(unclosed"},
    {"pattern": "x", "output_mode": "everything"},
    {"pattern": "x", "context": 11},
    {"pattern": "x", "max_results": 0},
])
def test_search_rejects_bad_arguments(repo, args):
    with pytest.raises(ValueError):
        validate_tool(_context(repo), "search", args)


# ---- glob -----------------------------------------------------------------

def test_glob_follows_path_glob_semantics_and_gitignore(repo):
    ctx = _context(repo)

    top_level = tool_glob(ctx, {"pattern": "*.md"}).splitlines()
    recursive = set(tool_glob(ctx, {"pattern": "**/*.py"}).splitlines())

    assert top_level == ["README.md"]
    assert recursive == {"pkg/__init__.py", "pkg/filters.py", "pkg/sub/deep.py"}  # 不含 node_modules


# ---- repo_map -------------------------------------------------------------

def test_repo_map_lists_classes_methods_and_signatures(repo):
    output = tool_repo_map(_context(repo), {"path": "pkg/filters.py"})

    assert "pkg/filters.py" in output
    assert "4: def floatformat(text, arg=-1)" in output
    assert "9: class Formatter(Base)" in output
    assert "        10: def format(self, value)" in output  # 方法多缩进一层
    assert "async def aformat(self, value)" in output


def test_repo_map_pattern_is_go_to_definition(repo):
    output = tool_repo_map(_context(repo), {"pattern": "^floatformat$"})

    assert output == "pkg/filters.py:4: def floatformat(text, arg=-1)"


def test_repo_map_reuses_parsed_files_until_they_change(repo):
    ctx = _context(repo)
    target = (repo / "pkg" / "sub" / "deep.py").resolve()
    tool_repo_map(ctx, {"pattern": "FloatFormat"})
    assert target in _SYMBOL_CACHE

    target.write_text("def renamed():\n    pass\n", encoding="utf-8")
    import os
    os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 5))

    assert "renamed" in tool_repo_map(ctx, {"pattern": "renamed"})


def test_repo_map_skips_unparseable_files_and_explains_non_python(repo, tmp_path):
    (repo / "pkg" / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.js").write_text("function f() {}\n", encoding="utf-8")

    assert "floatformat" in tool_repo_map(_context(repo), {"pattern": "floatformat"})
    assert "only understands Python" in tool_repo_map(_context(tmp_path), {"path": "web"})
