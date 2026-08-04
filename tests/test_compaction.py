"""
测试 compaction.py 各层压缩逻辑。
不需要真实 LLM 的部分直接测；需要 LLM 的用 mock。
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# 确保 hh_agent 在导入路径中
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── 测试用辅助函数 ──────────────────────────────────────
def make_msg(role, content="", tool_call_id=None, tool_calls=None):
    """构造一条标准消息。"""
    m = {"role": role, "content": content}
    if tool_call_id:
        m["tool_call_id"] = tool_call_id
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def make_tc_msg(content="", tool_calls=None):
    """构造一条带 tool_calls 的 assistant 消息。"""
    return make_msg("assistant", content=content, tool_calls=tool_calls or [])


def make_tool_msg(content="", tool_call_id="tc_1"):
    """构造一条 tool 应答消息。"""
    return make_msg("tool", content=content, tool_call_id=tool_call_id)


def build_conversation(n_turns):
    """构造 n 轮对话（每轮 user → assistant → 可能的 tool），返回 messages 列表。"""
    msgs = [make_msg("system", "你是一个编程助手")]
    for i in range(1, n_turns + 1):
        msgs.append(make_msg("user", f"用户问题 {i}"))
        msgs.append(make_msg("assistant", f"回答 {i}"))
    return msgs


# ═══════════════════════════════════════════════════════
#  estimate_token_count
# ═══════════════════════════════════════════════════════
class TestEstimateTokenCount:
    def test_empty(self):
        from hh_agent.compaction import estimate_token_count
        assert estimate_token_count([]) == 0

    def test_rough_estimate(self):
        from hh_agent.compaction import estimate_token_count
        msgs = [make_msg("user", "hello" * 100)]  # 500 chars → ~166 tokens
        est = estimate_token_count(msgs)
        assert 100 < est < 250


# ═══════════════════════════════════════════════════════
#  tool_result_budget (L3) — 大结果落盘
# ═══════════════════════════════════════════════════════
class TestToolResultBudget:
    def test_under_budget_untouched(self, tmp_path):
        from hh_agent import compaction
        compaction.PERSIST_DIR = tmp_path / "persist"
        msgs = [
            make_msg("system", "sys"),
            make_msg("user", "hi"),
            make_tool_msg("短输出"),
        ]
        result = compaction.tool_result_budget(msgs, max_bytes=1000)
        assert result[2]["content"] == "短输出"  # 不变

    def test_over_budget_persists_largest(self, tmp_path):
        from hh_agent import compaction
        compaction.PERSIST_DIR = tmp_path / "persist"
        short = "短" * 50      # 50 chars
        long = "长" * 5000     # 5000 chars
        msgs = [
            make_msg("system", "sys"),
            make_tool_msg(short, tool_call_id="tc_s"),
            make_tool_msg(long, tool_call_id="tc_l"),
        ]
        # max_bytes 设在 short 和 persisted-output 长度之间
        # persisted-output 约 2000+ chars，所以设 3000：长结果落盘后 total ≈ 2150 < 3000
        result = compaction.tool_result_budget(msgs, max_bytes=3000)
        tc_s_content = result[1]["content"]
        tc_l_content = result[2]["content"]
        # 长的那条应该变成 persisted-output
        assert "<persisted-output" in tc_l_content, f"长结果应落盘，实际: {tc_l_content[:100]}"
        # 短的应该保持不变
        assert tc_s_content == short, f"短结果不应被改，实际: {tc_s_content[:100]}"

    def test_already_persisted_skipped(self, tmp_path):
        from hh_agent import compaction
        compaction.PERSIST_DIR = tmp_path / "persist"
        msgs = [
            make_msg("system", "sys"),
            make_tool_msg("<persisted-output path=\"x\">\n预览\n</persisted-output>"),
            make_tool_msg("长" * 5000),
        ]
        result = compaction.tool_result_budget(msgs, max_bytes=500)
        # 第一条 tool 已落盘，跳过；第二条被落盘
        assert "<persisted-output" in result[1]["content"]  # 还是原样
        assert "<persisted-output" in result[2]["content"]  # 新落盘的


# ═══════════════════════════════════════════════════════
#  snip_compact (L1) — 裁掉中间
# ═══════════════════════════════════════════════════════
class TestSnipCompact:
    def test_short_no_cut(self):
        from hh_agent.compaction import snip_compact
        msgs = build_conversation(5)  # 1 + 5*2 = 11 条
        result = snip_compact(msgs, max_messages=50)
        assert len(result) == len(msgs)  # 不变

    def test_long_conversation_cuts_middle(self):
        from hh_agent.compaction import snip_compact
        msgs = build_conversation(40)  # 1 + 40*2 = 81 条
        result = snip_compact(msgs, max_messages=30)
        assert len(result) < len(msgs)
        # 头尾应该保留
        assert result[0]["role"] == "system"
        assert result[-1]["role"] == "assistant"
        # 中间应该有一条占位消息
        placeholders = [m for m in result if "已裁掉对话中间" in str(m.get("content", ""))]
        assert len(placeholders) == 1

    def test_head_has_tool_calls_expands(self):
        """头部最后一条是带 tool_calls 的 assistant，应该把后续 tool 消息也包进 head。"""
        from hh_agent.compaction import snip_compact
        tc = [{"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
        msgs = [
            make_msg("system", "sys"),
            make_msg("user", "u1"),
            make_tc_msg("a1", tool_calls=tc),     # index 2, head_end 初始=3, 这条在 head 最后
            make_tool_msg("结果1", tool_call_id="tc1"),  # index 3, 应被包进 head
            make_tool_msg("结果2", tool_call_id="tc2"),  # index 4, 也应被包进
            *build_conversation(30),
        ]
        result = snip_compact(msgs, max_messages=20)
        # head 部分应该包含 tool 应答
        head_tool_ids = [m.get("tool_call_id") for m in result[:6] if m.get("tool_call_id")]
        assert "tc1" in head_tool_ids

    def test_tail_starts_with_tool_retreats(self):
        """tail 开头是孤立的 tool 消息，应该退到包含它的 assistant。"""
        from hh_agent.compaction import snip_compact
        tc = [{"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
        msgs = build_conversation(30) + [
            make_tc_msg("带工具调用的回答", tool_calls=tc),
            make_tool_msg("工具结果", tool_call_id="tc1"),
            make_msg("user", "最终问题"),
            make_msg("assistant", "最终回答"),
        ]
        result = snip_compact(msgs, max_messages=15)
        # 尾部不应出现孤立的 tool 消息（它的 assistant 也该在尾部）
        tail_start_idx = None
        for i, m in enumerate(result):
            if "已裁掉对话中间" in str(m.get("content", "")):
                tail_start_idx = i + 1
                break
        if tail_start_idx:
            tail = result[tail_start_idx:]
            # 如果 tail 第一条是 tool，那它的 assistant 也一定在 tail 里（在它前面）
            if tail and tail[0]["role"] == "tool":
                # 则应能找到前面的 assistant
                has_assistant_before = any(
                    m["role"] == "assistant" and m.get("tool_calls")
                    for m in tail[:5]
                )
                assert has_assistant_before, "孤立的 tool 消息在 tail 开头"


# ═══════════════════════════════════════════════════════
#  micro_compact (L2) — 旧 tool_result 替换为占位符
# ═══════════════════════════════════════════════════════
class TestMicroCompact:
    def test_few_tools_untouched(self):
        from hh_agent import compaction
        old_keep = compaction.KEEP_RECENT_TOOL_RESULTS
        compaction.KEEP_RECENT_TOOL_RESULTS = 3
        try:
            msgs = [
                make_msg("system", "sys"),
                make_tool_msg("结果1"),
                make_tool_msg("结果2"),
            ]
            result = compaction.micro_compact(msgs)
            assert result[1]["content"] == "结果1"
            assert result[2]["content"] == "结果2"
        finally:
            compaction.KEEP_RECENT_TOOL_RESULTS = old_keep

    def test_old_tools_replaced(self):
        from hh_agent import compaction
        old_keep = compaction.KEEP_RECENT_TOOL_RESULTS
        compaction.KEEP_RECENT_TOOL_RESULTS = 2
        try:
            msgs = [
                make_msg("system", "sys"),
                make_tool_msg("旧结果 " + "x" * 200),   # 旧的，长内容
                make_tool_msg("旧结果2 " + "y" * 200),  # 旧的
                make_tool_msg("新结果1"),                # 保留
                make_tool_msg("新结果2"),                # 保留
            ]
            result = compaction.micro_compact(msgs)
            # 旧的两条应该变成占位符
            assert result[1]["content"] == compaction.MICRO_PLACEHOLDER
            assert result[2]["content"] == compaction.MICRO_PLACEHOLDER
            # 新的两条不变
            assert result[3]["content"] == "新结果1"
            assert result[4]["content"] == "新结果2"
        finally:
            compaction.KEEP_RECENT_TOOL_RESULTS = old_keep

    def test_short_content_not_replaced(self):
        """短内容即使旧也不替换（不值得）。"""
        from hh_agent import compaction
        old_keep = compaction.KEEP_RECENT_TOOL_RESULTS
        compaction.KEEP_RECENT_TOOL_RESULTS = 1
        try:
            msgs = [
                make_msg("system", "sys"),
                make_tool_msg("短"),        # 旧，但短(≤120)不替换
                make_tool_msg("新结果"),
            ]
            result = compaction.micro_compact(msgs)
            assert result[1]["content"] == "短"  # 保留
        finally:
            compaction.KEEP_RECENT_TOOL_RESULTS = old_keep


# ═══════════════════════════════════════════════════════
#  preprocess_context — 管线顺序 + 无 API 调用
# ═══════════════════════════════════════════════════════
class TestPreprocessContext:
    def test_runs_without_error(self):
        from hh_agent.compaction import preprocess_context
        msgs = build_conversation(10)
        result = preprocess_context(msgs)
        assert len(result) > 0
        assert result[0]["role"] == "system"

    def test_does_not_call_llm(self):
        """预处理器不应产生任何 API 调用。"""
        from hh_agent.compaction import preprocess_context
        msgs = build_conversation(10)
        # 不需要 mock —— 如果调了 LLM 会因为没有真实 key 而报错
        result = preprocess_context(msgs)
        assert len(result) <= len(msgs) + 1


# ═══════════════════════════════════════════════════════
#  compact_history — 需要 mock LLM
# ═══════════════════════════════════════════════════════
class TestCompactHistory:
    def test_preserves_system_message(self):
        from hh_agent.compaction import compact_history, summarize_history
        msgs = build_conversation(20)
        with patch("hh_agent.compaction.summarize_history", return_value="这是一段摘要"):
            result = compact_history(msgs)
        assert result[0]["role"] == "system"

    def test_includes_summary_as_user_message(self):
        from hh_agent.compaction import compact_history
        msgs = build_conversation(20)
        expected_summary = "摘要：做了很多事情"
        with patch("hh_agent.compaction.summarize_history", return_value=expected_summary):
            result = compact_history(msgs)
        assert result[1]["role"] == "user"
        assert expected_summary in result[1]["content"]
        assert "[历史已压缩]" in result[1]["content"]

    def test_keeps_tail_messages(self):
        """验证尾部消息被保留。"""
        from hh_agent.compaction import compact_history
        msgs = build_conversation(20)
        # 最后一条 user 消息应该是 "用户问题 20"
        assert msgs[-2]["content"] == "用户问题 20"
        with patch("hh_agent.compaction.summarize_history", return_value="摘要"):
            result = compact_history(msgs)
        # 系统 + 摘要 + tail
        assert len(result) > 2, f"应保留尾部消息，实际只有 {len(result)} 条"
        # 尾部应包含最后几条原始消息
        assert len(result) >= 3

    def test_no_orphan_tool_in_tail(self):
        """尾部开头不应有孤立的 tool 消息。"""
        from hh_agent.compaction import compact_history
        tc = [{"id": "tx", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
        # 构造场景：倒数第3条是带 tc 的 assistant，倒数第2条是 tool，倒数第1条是 user
        msgs = build_conversation(15) + [
            make_tc_msg("带工具", tool_calls=tc),
            make_tool_msg("工具结果", tool_call_id="tx"),
            make_msg("user", "最终问题"),
        ]
        with patch("hh_agent.compaction.summarize_history", return_value="摘要"):
            result = compact_history(msgs)
        # 检查尾部：系统+摘要 之后的部分
        tail = result[2:]
        if tail and tail[0]["role"] == "tool":
            # 如果 tail 开头是 tool，它对应的 assistant 也必须在 tail 中
            has_owner = any(
                m["role"] == "assistant" and m.get("tool_calls")
                for m in tail
            )
            assert has_owner, f"尾部有孤立的 tool 消息！tail={tail[:3]}"


# ═══════════════════════════════════════════════════════
#  reactive_compact — 应急压缩（mock LLM）
# ═══════════════════════════════════════════════════════
class TestReactiveCompact:
    def test_preserves_system_and_tail(self):
        from hh_agent.compaction import reactive_compact
        msgs = build_conversation(25)
        with patch("hh_agent.compaction.summarize_history", return_value="应急摘要"):
            result = reactive_compact(msgs)
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"
        assert "应急压缩" in result[1]["content"]
        # 应保留尾部（> 2 条）
        assert len(result) > 2

    def test_no_orphan_tool_in_tail(self):
        from hh_agent.compaction import reactive_compact
        tc = [{"id": "tx", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
        msgs = build_conversation(15) + [
            make_tc_msg("tc_assistant", tool_calls=tc),
            make_tool_msg("tc_result", tool_call_id="tx"),
            make_msg("user", "新问题"),
        ]
        with patch("hh_agent.compaction.summarize_history", return_value="应急摘要"):
            result = reactive_compact(msgs)
        tail = result[2:]
        if tail and tail[0]["role"] == "tool":
            has_owner = any(
                m["role"] == "assistant" and m.get("tool_calls")
                for m in tail
            )
            assert has_owner, f"应急压缩尾部有孤立的 tool 消息！"


# ═══════════════════════════════════════════════════════
#  边界情况
# ═══════════════════════════════════════════════════════
class TestEdgeCases:
    def test_very_short_conversation_no_crash(self):
        """3 条消息的对话不应崩溃。"""
        from hh_agent.compaction import snip_compact, micro_compact, tool_result_budget
        msgs = [
            make_msg("system", "sys"),
            make_msg("user", "hi"),
            make_msg("assistant", "hello"),
        ]
        # 所有函数都不应崩溃
        tool_result_budget(msgs, max_bytes=100)
        snip_compact(msgs, max_messages=50)
        micro_compact(msgs)

    def test_only_system_message(self):
        from hh_agent.compaction import preprocess_context
        msgs = [make_msg("system", "sys")]
        result = preprocess_context(msgs)
        assert len(result) == 1

    def test_many_tool_messages_consecutive(self):
        """连续 20 条 tool 消息不应崩溃。"""
        from hh_agent.compaction import snip_compact, micro_compact
        msgs = [make_msg("system", "sys")]
        for i in range(20):
            msgs.append(make_tool_msg(f"结果{i}", tool_call_id=f"tc_{i}"))
        msgs.append(make_msg("user", "继续"))
        msgs.append(make_msg("assistant", "好的"))
        # 不应崩溃
        snip_compact(msgs, max_messages=10)
        result = micro_compact(msgs)
        assert len(result) == len(msgs)

    def test_compact_history_with_short_conversation(self):
        """短对话触发 compact 时不应崩溃。"""
        from hh_agent.compaction import compact_history
        msgs = [
            make_msg("system", "sys"),
            make_msg("user", "一个问题"),
            make_msg("assistant", "一个回答"),
        ]
        with patch("hh_agent.compaction.summarize_history", return_value="短摘要"):
            result = compact_history(msgs)
        assert len(result) >= 2
        assert result[0]["role"] == "system"
