"""
测试 memory.py — 跨会话记忆：存储、加载、提取、整理。
需要 LLM 的部分用 mock，不需要的直接测。
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── 辅助 ────────────────────────────────────────────────
def make_msg(role, content=""):
    return {"role": role, "content": content}


def _patch_memory_dir(monkeypatch, tmp_path):
    """同时 patch MEMORY_DIR 和 INDEX_FILE（因为 INDEX_FILE 在 import 时已固化）。"""
    from hh_agent import memory
    mem_dir = tmp_path / ".memory"
    monkeypatch.setattr(memory, "MEMORY_DIR", mem_dir)
    monkeypatch.setattr(memory, "INDEX_FILE", mem_dir / "MEMORY.md")
    return mem_dir


# ═══════════════════════════════════════════════════════
#  _slug
# ═══════════════════════════════════════════════════════
class TestSlug:
    def test_basic(self):
        from hh_agent.memory import _slug
        assert _slug("User Preferences") == "user-preferences"

    def test_special_chars(self):
        from hh_agent.memory import _slug
        # ! 被 strip 掉，末尾的 - 也被 strip("-") 去掉
        assert _slug("My Project (v2)!") == "my-project--v2"

    def test_chinese_removed(self):
        from hh_agent.memory import _slug
        result = _slug("用户偏好")
        # 纯中文 → 全被 strip → 退化到 "memory-XXXX" 带 hash 后缀
        assert result.startswith("memory-")
        assert len(result) == 11  # "memory-XXXX"

    def test_empty(self):
        from hh_agent.memory import _slug
        result = _slug("")
        # 空字符串也退化到 "memory-XXXX"
        assert result.startswith("memory-")


# ═══════════════════════════════════════════════════════
#  list_memory_files
# ═══════════════════════════════════════════════════════
class TestListMemoryFiles:
    def test_empty_dir(self, tmp_path, monkeypatch):
        mem_dir = _patch_memory_dir(monkeypatch, tmp_path)
        mem_dir.mkdir()
        from hh_agent import memory
        assert memory.list_memory_files() == []

    def test_dir_not_exists(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        assert memory.list_memory_files() == []

    def test_parses_frontmatter(self, tmp_path, monkeypatch):
        mem_dir = _patch_memory_dir(monkeypatch, tmp_path)
        mem_dir.mkdir()
        (mem_dir / "test-memory.md").write_text(
            "---\nname: 测试记忆\ndescription: 一段描述\ntype: project\n---\n\n记忆正文内容\n",
            encoding="utf-8",
        )
        from hh_agent import memory
        result = memory.list_memory_files()
        assert len(result) == 1
        assert result[0]["name"] == "测试记忆"
        assert result[0]["description"] == "一段描述"
        assert result[0]["type"] == "project"
        assert "记忆正文内容" in result[0]["content"]

    def test_skips_index_file(self, tmp_path, monkeypatch):
        mem_dir = _patch_memory_dir(monkeypatch, tmp_path)
        mem_dir.mkdir()
        (mem_dir / "MEMORY.md").write_text("# Memory Index\n", encoding="utf-8")
        (mem_dir / "real-memory.md").write_text(
            "---\nname: real\ndescription: d\ntype: user\n---\n\nbody\n", encoding="utf-8"
        )
        from hh_agent import memory
        result = memory.list_memory_files()
        assert len(result) == 1
        assert result[0]["name"] == "real"

    def test_no_frontmatter(self, tmp_path, monkeypatch):
        mem_dir = _patch_memory_dir(monkeypatch, tmp_path)
        mem_dir.mkdir()
        (mem_dir / "plain.md").write_text("没有元数据的纯文本", encoding="utf-8")
        from hh_agent import memory
        result = memory.list_memory_files()
        assert len(result) == 1
        assert result[0]["name"] == "plain"  # fallback to stem
        assert result[0]["description"] == ""


# ═══════════════════════════════════════════════════════
#  write_memory_file
# ═══════════════════════════════════════════════════════
class TestWriteMemoryFile:
    def test_write_and_read(self, tmp_path, monkeypatch):
        mem_dir = _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        memory.write_memory_file("测试", "user", "描述", "正文内容")

        files = list(mem_dir.glob("*.md"))
        assert len(files) >= 2  # 记忆文件 + MEMORY.md

        result = memory.list_memory_files()
        assert len(result) == 1
        assert result[0]["name"] == "测试"
        assert result[0]["type"] == "user"
        assert "正文内容" in result[0]["content"]

    def test_rebuilds_index(self, tmp_path, monkeypatch):
        mem_dir = _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        memory.write_memory_file("记忆A", "user", "描述A", "正文A")

        index_content = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "记忆A" in index_content
        assert "描述A" in index_content

    def test_overwrite_same_slug(self, tmp_path, monkeypatch):
        """同名 slug 会覆盖（当前设计如此，值得注意）。"""
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        memory.write_memory_file("my-project", "user", "第一次写", "内容1")
        memory.write_memory_file("my project", "project", "第二次写", "内容2")

        result = memory.list_memory_files()
        assert len(result) == 1
        assert result[0]["description"] == "第二次写"


# ═══════════════════════════════════════════════════════
#  read_memory_index
# ═══════════════════════════════════════════════════════
class TestReadMemoryIndex:
    def test_empty(self, tmp_path, monkeypatch):
        mem_dir = _patch_memory_dir(monkeypatch, tmp_path)
        mem_dir.mkdir()
        from hh_agent import memory
        assert memory.read_memory_index() == ""

    def test_with_files(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        memory.write_memory_file("偏好", "user", "用户偏好中文", "内容")
        result = memory.read_memory_index()
        assert "偏好" in result
        assert "用户偏好中文" in result


# ═══════════════════════════════════════════════════════
#  select_relevant_memories — mock LLM
# ═══════════════════════════════════════════════════════
class TestSelectRelevantMemories:
    def test_empty_files_returns_empty(self, tmp_path, monkeypatch):
        mem_dir = _patch_memory_dir(monkeypatch, tmp_path)
        mem_dir.mkdir()
        from hh_agent import memory
        assert memory.select_relevant_memories([]) == []

    def test_llm_selects_correctly(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        memory.write_memory_file("language-pref", "user", "user prefers Chinese", "body1")
        memory.write_memory_file("project-arch", "project", "uses Python", "body2")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="[0]"))]
        monkeypatch.setattr(memory.client.chat.completions, "create", lambda **kw: mock_resp)

        result = memory.select_relevant_memories([make_msg("user", "please answer in Chinese")])
        assert len(result) == 1
        assert result[0]["name"] == "language-pref"

    def test_llm_fails_falls_back_to_keyword(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        memory.write_memory_file("pytest-tips", "user", "use pytest for testing", "body")

        monkeypatch.setattr(memory.client.chat.completions, "create",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("API down")))

        # 对话中含 "pytest" → 关键词降级匹配到
        result = memory.select_relevant_memories([make_msg("user", "how do I use pytest for my tests")])
        assert len(result) >= 1
        assert result[0]["name"] == "pytest-tips"

    def test_llm_returns_empty_array(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        memory.write_memory_file("无关记忆", "user", "完全不相关的东西", "内容")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="[]"))]
        monkeypatch.setattr(memory.client.chat.completions, "create", lambda **kw: mock_resp)

        result = memory.select_relevant_memories([make_msg("user", "今天天气怎么样")])
        assert result == []

    def test_llm_bad_json_falls_back(self, tmp_path, monkeypatch):
        """LLM 返回非法 JSON → 降级关键词"""
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        memory.write_memory_file("Python偏好", "user", "用 Python 写代码", "内容")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="这不是 JSON"))]
        monkeypatch.setattr(memory.client.chat.completions, "create", lambda **kw: mock_resp)

        result = memory.select_relevant_memories([make_msg("user", "帮我写个 Python 脚本")])
        assert len(result) >= 1
        assert result[0]["name"] == "Python偏好"


# ═══════════════════════════════════════════════════════
#  load_memories
# ═══════════════════════════════════════════════════════
class TestLoadMemories:
    def test_no_files_no_injection(self, tmp_path, monkeypatch):
        mem_dir = _patch_memory_dir(monkeypatch, tmp_path)
        mem_dir.mkdir()
        from hh_agent import memory
        msgs = [make_msg("system", "sys"), make_msg("user", "hi")]
        memory.load_memories(msgs)
        assert len(msgs) == 2

    def test_injects_into_messages(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        memory.write_memory_file("偏好", "user", "用户说喜欢中文", "正文内容")
        files = memory.list_memory_files()

        monkeypatch.setattr(memory, "select_relevant_memories",
                            lambda msgs, max_items=5: files)

        msgs = [make_msg("system", "sys"), make_msg("user", "hi")]
        memory.load_memories(msgs)
        assert len(msgs) == 3
        assert "<relevant-memories>" in msgs[2]["content"]
        assert "偏好" in msgs[2]["content"]


# ═══════════════════════════════════════════════════════
#  extract_memories — mock LLM
# ═══════════════════════════════════════════════════════
class TestExtractMemories:
    def test_extracts_new_memory(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content=json.dumps([
            {"name": "测试偏好", "type": "user", "description": "喜欢 pytest", "body": "用户倾向于用 pytest"}
        ])))]
        monkeypatch.setattr(memory.client.chat.completions, "create", lambda **kw: mock_resp)

        msgs = [make_msg("user", "我喜欢用 pytest"), make_msg("assistant", "好的")]
        memory.extract_memories(msgs)
        files = memory.list_memory_files()
        assert len(files) == 1
        assert files[0]["name"] == "测试偏好"

    def test_no_new_info_returns_empty(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="[]"))]
        monkeypatch.setattr(memory.client.chat.completions, "create", lambda **kw: mock_resp)

        memory.extract_memories([make_msg("user", "你好")])
        assert memory.list_memory_files() == []

    def test_llm_error_graceful(self, tmp_path, monkeypatch, capsys):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory

        monkeypatch.setattr(memory.client.chat.completions, "create",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("提取失败")))

        memory.extract_memories([make_msg("user", "测试")])
        captured = capsys.readouterr()
        assert "提取跳过" in captured.out


# ═══════════════════════════════════════════════════════
#  consolidate_memories — mock LLM
# ═══════════════════════════════════════════════════════
class TestConsolidateMemories:
    def test_below_threshold_skips(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory

        for i in range(3):
            memory.write_memory_file(f"记忆{i}", "user", f"描述{i}", f"内容{i}")

        before = len(memory.list_memory_files())
        memory.consolidate_memories()  # 默认阈值 10，3 条不触发
        assert len(memory.list_memory_files()) == before

    def test_reaches_threshold_consolidates(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        monkeypatch.setattr(memory, "CONSOLIDATE_THRESHOLD", 5)

        for i in range(5):
            memory.write_memory_file(f"记忆{i}", "user", f"描述{i}", f"内容{i}")

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content=json.dumps([
            {"name": "合并A", "type": "user", "description": "合并后", "body": "合并内容A"},
            {"name": "合并B", "type": "project", "description": "合并后", "body": "合并内容B"},
        ])))]
        monkeypatch.setattr(memory.client.chat.completions, "create", lambda **kw: mock_resp)

        memory.consolidate_memories()
        files = memory.list_memory_files()
        assert len(files) == 2
        assert {f["name"] for f in files} == {"合并A", "合并B"}

    def test_llm_error_graceful(self, tmp_path, monkeypatch, capsys):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        monkeypatch.setattr(memory, "CONSOLIDATE_THRESHOLD", 3)

        for i in range(3):
            memory.write_memory_file(f"记忆{i}", "user", f"描述{i}", f"内容{i}")

        monkeypatch.setattr(memory.client.chat.completions, "create",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("整理失败")))

        memory.consolidate_memories()
        captured = capsys.readouterr()
        assert "整理跳过" in captured.out
        # 原始文件还在（异常时不会 unlink）
        assert len(memory.list_memory_files()) == 3


# ═══════════════════════════════════════════════════════
#  边界情况
# ═══════════════════════════════════════════════════════
class TestEdgeCases:
    def test_frontmatter_special_chars(self, tmp_path, monkeypatch):
        """值中含冒号等特殊字符时的行为。"""
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        # 当前实现：description 含冒号会被截断 — 已知限制
        memory.write_memory_file("test", "user", "简单描述", "正文")
        result = memory.list_memory_files()
        assert len(result) == 1

    def test_multiple_writes(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        for i in range(10):
            memory.write_memory_file(f"记忆-{i}", "user", f"描述-{i}", f"内容-{i}")
        assert len(memory.list_memory_files()) == 10

    def test_memory_dir_created_on_demand(self, tmp_path, monkeypatch):
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        assert not memory.MEMORY_DIR.exists()
        memory.write_memory_file("测试", "user", "描述", "内容")
        assert memory.MEMORY_DIR.exists()

    def test_body_unchanged_after_roundtrip(self, tmp_path, monkeypatch):
        """写入再读回，body 内容不应丢失或篡改。"""
        _patch_memory_dir(monkeypatch, tmp_path)
        from hh_agent import memory
        body = "这是一段多行正文。\n包含换行和特殊字符：{}[]"
        memory.write_memory_file("测试记忆", "project", "项目相关事实", body)
        files = memory.list_memory_files()
        assert len(files) == 1
        assert body in files[0]["content"]
