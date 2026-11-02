"""组装每次模型请求：稳定的 system + 只追加的会话记录 + 末尾一条小提醒。

    system    规则、工具策略、项目指令（AGENTS.md / CLAUDE.md 完整注入）、会话开始时的环境快照、
              长期记忆与 skills 索引。整个 Session 内不变，前缀缓存可以一直命中。
    messages  Session 历史原样转成 Chat Completions 消息（assistant/tool 成对），只追加不重写；
              之前做过摘要压缩时，最前面是一条摘要。
    reminder  最后一条 user 消息里的 <system-reminder>：任务进度、todo、运行时指令。每轮都变，
              放在最后，不影响前面的缓存。

预算按 token 计，由模型窗口推导，并和一个成本上的"工作预算"取较小值：

    budget = min(working_budget, context_window - max_new_tokens - margin)

只在接近预算时压缩，而不是每轮裁剪：
    > light_ratio × budget    轻压缩：较早的大块工具结果换成占位符（不调用模型）
    > compact_ratio × budget  摘要压缩：让模型把较早的轮次写成结构化摘要，最近几轮原样保留
    仍超出 budget             兜底：从最旧的轮次开始丢弃，工具调用对不拆开

压缩状态存在 session["context"]，历史本身不删除（session_search 与断点恢复仍可用）。
"""

from __future__ import annotations

import json
import math
import re

from .providers.base import ModelRequest
from .task_state import VERIFICATION_REPAIR_STEPS
from .workspace import MAX_TOOL_OUTPUT, clip_head_tail

# 成本上的工作预算：1M 窗口不意味着每次都该发 1M token。
DEFAULT_WORKING_BUDGET_TOKENS = 128_000
LIGHT_COMPACTION_RATIO = 0.6
SUMMARY_COMPACTION_RATIO = 0.8
# 轻压缩保留最近多少条工具结果原文；更早且超过这个长度的才会被清理。
KEEP_RECENT_TOOL_RESULTS = 8
CLEARABLE_TOOL_RESULT_CHARS = 800
# 摘要压缩时，最近的轮次原样保留，最多占预算的这一比例。
SUMMARY_KEEP_RECENT_RATIO = 0.25
SUMMARY_MAX_TOKENS = 4096
# 单条文本消息的上限：工具结果在执行时已按 MAX_TOOL_OUTPUT 截断，这里只防极端情况。
MESSAGE_CHAR_LIMIT = MAX_TOOL_OUTPUT

CLEARED_TOOL_RESULT = "[Earlier tool output ({chars} chars) cleared to save context. Re-run the tool if you need it again.]"
SUMMARY_PREFIX = "Summary of the earlier part of this conversation (older turns were compacted):\n\n"

SUMMARY_SYSTEM_PROMPT = """You compact the conversation of a coding agent so it can keep working with less context.
Write a faithful, specific summary. Keep file paths, function names, commands, error messages and decisions verbatim.
Do not invent anything. Use exactly these sections:

1. User request and goals
2. Key decisions and constraints
3. Files read or changed, and what was learned or done
4. Errors encountered and how they were resolved
5. Current state and next steps"""

_CJK = re.compile(r"[⺀-鿿가-힯豈-﫿＀-￯]")


def estimate_tokens(text):
    """粗略估算 token：CJK 约 1 字 1 token，其余约 3.5 字符 1 token。真实值用 API usage 校准。"""
    text = str(text or "")
    cjk = len(_CJK.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 3.5)


def _message_tokens(message):
    return estimate_tokens(json.dumps(message, ensure_ascii=False)) + 4


class ContextManager:
    def __init__(
        self,
        agent,
        working_budget_tokens=DEFAULT_WORKING_BUDGET_TOKENS,
        light_ratio=LIGHT_COMPACTION_RATIO,
        compact_ratio=SUMMARY_COMPACTION_RATIO,
    ):
        self.agent = agent
        self.working_budget_tokens = int(working_budget_tokens)
        self.light_ratio = float(light_ratio)
        self.compact_ratio = float(compact_ratio)
        # 本地估算 × calibration ≈ API 返回的真实 prompt_tokens。
        self.calibration = 1.0
        self._last_estimate = 0

    # ---- 预算 ---------------------------------------------------------------

    def budget_tokens(self):
        window = int(getattr(self.agent.model_client, "context_window", 128_000))
        reserve = int(self.agent.max_new_tokens) + max(4096, window // 50)
        return max(1024, min(self.working_budget_tokens, window - reserve))

    def estimate(self, system_prompt, messages):
        raw = estimate_tokens(system_prompt) + sum(_message_tokens(message) for message in messages)
        return int(raw * self.calibration)

    def observe_usage(self, completion_metadata):
        """用 API 返回的真实 prompt_tokens 校准估算（指数平滑，限制在 0.5~2 倍）。"""
        actual = int((completion_metadata or {}).get("input_tokens") or 0)
        if actual <= 0 or self._last_estimate <= 0:
            return
        ratio = actual / (self._last_estimate / self.calibration)
        self.calibration = min(2.0, max(0.5, 0.5 * self.calibration + 0.5 * ratio))

    # ---- 三层内容 -------------------------------------------------------------

    def _state(self):
        state = self.agent.session.setdefault("context", {})
        state.setdefault("summary", "")
        state.setdefault("summary_upto", -1)
        state.setdefault("cleared", [])
        return state

    def system_prompt(self):
        parts = [self.agent.prefix]
        if self.agent.feature_enabled("memory"):
            parts.append(str(self.agent.durable_memory_text()))
        if self.agent.feature_enabled("skills"):
            parts.append(str(self.agent.skills_text()))
        return "\n\n".join(part for part in parts if part.strip())

    def _visible_history(self):
        state = self._state()
        upto = int(state["summary_upto"])
        cleared = set(state["cleared"])
        items = []
        for item in self.agent.session.get("history", []):
            # 手工构造的条目可能没有序号：视为普通可见条目，不参与压缩。
            sequence = item.get("archive_sequence")
            if sequence is not None and int(sequence) <= upto:
                continue
            if item.get("role") == "tool" and sequence is not None and int(sequence) in cleared:
                item = {**item, "content": CLEARED_TOOL_RESULT.format(chars=len(str(item.get("content", ""))))}
            items.append(item)
        return items

    def transcript_messages(self):
        messages = []
        summary = self._state()["summary"]
        if summary:
            messages.append({"role": "user", "content": SUMMARY_PREFIX + summary})
        messages.extend(self._to_messages(self._visible_history()))
        return messages

    def _to_messages(self, history):
        """Session 条目 → Chat Completions 消息；assistant 的 tool_calls 必须紧跟对应的 tool 结果。"""
        needs_reasoning = bool(getattr(self.agent.model_client, "requires_reasoning_content_for_tool_calls", False))
        messages = []
        known_calls = set()
        for item in history:
            role = str(item.get("role", ""))
            calls = item.get("tool_calls") or ([item["tool_call"]] if item.get("tool_call") else [])
            if role == "assistant" and calls:
                reasoning = str(item.get("reasoning_content", "") or "")
                if needs_reasoning and not reasoning:
                    # thinking 模式之前写下的会话缺少 reasoning_content，无法合法重放这次调用；
                    # 它的工具结果会在下面降级成普通观察消息。
                    continue
                native = []
                for call in calls:
                    call_id = str(call.get("id", ""))
                    if not call_id:
                        continue
                    known_calls.add(call_id)
                    native.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(call.get("name", "")),
                            "arguments": json.dumps(call.get("args", {}), ensure_ascii=False, sort_keys=True),
                        },
                    })
                if native:
                    message = {"role": "assistant", "content": str(item.get("content", "")).strip() or None, "tool_calls": native}
                    if reasoning:
                        # DeepSeek thinking 模式要求原样回传，不能截断或改写。
                        message["reasoning_content"] = reasoning
                    messages.append(message)
                continue
            if role == "tool":
                content = clip_head_tail(item.get("content", ""), MESSAGE_CHAR_LIMIT)
                call_id = str(item.get("tool_call_id", ""))
                if call_id in known_calls:
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
                else:
                    messages.append({"role": "user", "content": f"Tool observation [{item.get('name', '')}]:\n{content}"})
                continue
            if role in {"user", "assistant"}:
                content = str(item.get("content", "")).strip()
                if content:
                    messages.append({"role": role, "content": clip_head_tail(content, MESSAGE_CHAR_LIMIT)})
        return messages

    def reminder(self, user_message):
        agent = self.agent
        lines = []
        # 断点恢复信息放在末尾提醒里：每次 checkpoint 都会变，放在开头会让整段缓存失效。
        checkpoint = str(agent.render_checkpoint_text() or "").strip()
        if checkpoint:
            lines.append(checkpoint)
        lines.append(str(agent.task_state.render()))
        run_state = getattr(agent, "current_run_state", None)
        if run_state is not None:
            max_steps = int(agent.max_steps)
            if agent.task_state.to_dict().get("verification_pending", False):
                max_steps += VERIFICATION_REPAIR_STEPS
            lines.append(
                f"Run progress: decisions {int(run_state.attempts)}, tool steps {int(run_state.tool_steps)}/{max_steps}."
            )
        # 当前请求已被摘要压缩掉时，原文放回提醒里，保证目标完整。
        if self._state()["summary"] and not any(
            item.get("role") == "user" and str(item.get("content", "")).strip() == str(user_message).strip()
            for item in self._visible_history()
        ):
            lines.append("Current user request:\n" + str(user_message))
        if bool(getattr(agent, "output_recovery_active", False)):
            draft = clip_head_tail(getattr(agent, "output_recovery_draft", ""), 6000)
            lines.append(
                "Runtime output recovery (authoritative): the previous response reached its output-token limit. "
                "Return only the missing continuation as a final answer. Do not repeat the draft and do not request tools."
                + (f"\nPartial draft to continue exactly:\n{draft}" if draft else "")
            )
        correction = str(getattr(agent, "model_output_correction", "") or "").strip()
        if correction:
            lines.append("Runtime format correction (authoritative): " + correction)
        if bool(getattr(agent, "final_only_mode", False)):
            lines.append(
                "Runtime directive (authoritative): tools are disabled for this decision. "
                "Return final, needs_user_input, or blocked using existing evidence."
            )
        return "<system-reminder>\n" + "\n\n".join(line for line in lines if line.strip()) + "\n</system-reminder>"

    @staticmethod
    def _append_reminder(messages, reminder):
        # 历史最后一条是 user 时拼在它后面，避免连续两条 user 消息。
        if messages and messages[-1].get("role") == "user":
            messages[-1] = {**messages[-1], "content": f"{messages[-1]['content']}\n\n{reminder}"}
        else:
            messages.append({"role": "user", "content": reminder})
        return messages

    def _with_reminder(self, user_message):
        return self._append_reminder(self.transcript_messages(), self.reminder(user_message))

    # ---- 组装与压缩 -----------------------------------------------------------

    def prepare(self, user_message, tools=None):
        """返回 (ModelRequest, metadata)。需要时先压缩，再组装。"""
        system_prompt = self.system_prompt()
        budget = self.budget_tokens()
        compaction = None
        if self.agent.feature_enabled("context_reduction"):
            total = self.estimate(system_prompt, self._with_reminder(user_message))
            if total > self.compact_ratio * budget and self.agent.feature_enabled("context_summary"):
                compaction = self._summarize(budget)
            elif total > self.light_ratio * budget:
                compaction = self._clear_old_tool_results()
        messages = self._with_reminder(user_message)
        total = self.estimate(system_prompt, messages)
        if total > budget:
            # 兜底：摘要不可用或仍超出时，从最旧的轮次开始丢弃，保证请求不超出窗口。
            before = (compaction or {}).get("before_tokens", total)
            messages = self._trim_oldest(messages, budget - estimate_tokens(system_prompt))
            total = self.estimate(system_prompt, messages)
            kind = f"{compaction['kind']}+trim" if compaction else "trim"
            compaction = {**(compaction or {}), "kind": kind, "before_tokens": before, "after_tokens": total}
        self._last_estimate = total
        request = ModelRequest(
            system_prompt=system_prompt,
            messages=messages,
            max_new_tokens=int(self.agent.max_new_tokens),
            tools=tools,
        )
        state = self._state()
        metadata = {
            "estimated_input_tokens": total,
            "budget_tokens": budget,
            "context_window": int(getattr(self.agent.model_client, "context_window", 0)),
            "system_tokens": estimate_tokens(system_prompt),
            "message_count": len(messages),
            "summary_active": bool(state["summary"]),
            "cleared_tool_results": len(state["cleared"]),
            "calibration": round(self.calibration, 3),
            "compaction": compaction,
        }
        return request, metadata

    def _clear_old_tool_results(self):
        state = self._state()
        before = self.estimate(self.system_prompt(), self.transcript_messages())
        tool_items = [item for item in self._visible_history() if item.get("role") == "tool"]
        cleared = set(state["cleared"])
        newly = [
            int(item["archive_sequence"])
            for item in tool_items[:-KEEP_RECENT_TOOL_RESULTS]
            if item.get("archive_sequence") is not None
            and int(item["archive_sequence"]) not in cleared
            and len(str(item.get("content", ""))) > CLEARABLE_TOOL_RESULT_CHARS
        ]
        if not newly:
            return None
        state["cleared"] = sorted(cleared | set(newly))
        after = self.estimate(self.system_prompt(), self.transcript_messages())
        return {"kind": "light", "cleared": len(newly), "before_tokens": before, "after_tokens": after}

    @staticmethod
    def _units(history):
        """把历史切成不可拆分的单元：一次 assistant 工具调用连同它的全部结果算一个单元。"""
        units, index = [], 0
        while index < len(history):
            item = history[index]
            unit = [item]
            if item.get("role") == "assistant" and (item.get("tool_calls") or item.get("tool_call")):
                while index + 1 < len(history) and history[index + 1].get("role") == "tool":
                    index += 1
                    unit.append(history[index])
            units.append(unit)
            index += 1
        return units

    def _summarize(self, budget):
        state = self._state()
        units = self._units(self._visible_history())
        keep_budget = int(budget * SUMMARY_KEEP_RECENT_RATIO)
        kept, used = [], 0
        for unit in reversed(units):
            size = sum(_message_tokens(message) for message in self._to_messages(unit))
            if kept and used + size > keep_budget:
                break
            kept.insert(0, unit)
            used += size
        older = [item for unit in units[: len(units) - len(kept)] for item in unit]
        if not older or older[-1].get("archive_sequence") is None:
            return self._clear_old_tool_results()

        before = self.estimate(self.system_prompt(), self.transcript_messages())
        transcript = "\n\n".join(self._render_for_summary(item) for item in older)
        previous = f"Existing summary of even earlier turns:\n{state['summary']}\n\n" if state["summary"] else ""
        request = ModelRequest(
            system_prompt=SUMMARY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": previous + "Conversation to summarize:\n\n" + transcript}],
            max_new_tokens=SUMMARY_MAX_TOKENS,
        )
        try:
            summary = str(self.agent.model_client.complete(request) or "").strip()
        except Exception as exc:  # 摘要失败不能让任务失败：退回轻压缩 + 兜底裁剪
            self.agent.emit_ui("status", message=f"Context summary failed ({exc}); clearing old tool output instead.")
            return self._clear_old_tool_results()
        summary = summary.removeprefix("<final>").removesuffix("</final>").strip()
        if not summary:
            return self._clear_old_tool_results()
        usage = dict(getattr(self.agent.model_client, "last_completion_metadata", {}) or {})
        if usage:
            self.agent.emit_ui(
                "usage",
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_read_tokens=int(usage.get("cached_tokens", 0) or 0),
                cache_write_tokens=int(usage.get("cache_write_tokens", 0) or 0),
                requests=1,
            )
        state["summary"] = summary
        state["summary_upto"] = int(older[-1]["archive_sequence"])
        after = self.estimate(self.system_prompt(), self.transcript_messages())
        return {"kind": "summary", "summarized_items": len(older), "before_tokens": before, "after_tokens": after}

    @staticmethod
    def _render_for_summary(item):
        role = item.get("role", "")
        if role == "tool":
            return f"[tool result: {item.get('name', '')}]\n{clip_head_tail(item.get('content', ''), 4000)}"
        calls = item.get("tool_calls") or ([item["tool_call"]] if item.get("tool_call") else [])
        if calls:
            rendered = "; ".join(f"{call.get('name')}({json.dumps(call.get('args', {}), ensure_ascii=False)})" for call in calls)
            return f"[assistant tool call] {rendered}"
        return f"[{role}] {clip_head_tail(item.get('content', ''), 4000)}"

    def _trim_oldest(self, messages, budget):
        units, index = [], 0
        while index < len(messages):
            message = messages[index]
            unit = [message]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                ids = {call["id"] for call in message["tool_calls"]}
                while index + 1 < len(messages) and messages[index + 1].get("tool_call_id") in ids:
                    index += 1
                    unit.append(messages[index])
            units.append(unit)
            index += 1
        # 从最新往回装，最后一个单元带着本轮提醒，总会保留。
        selected, used = [], 0
        for unit in reversed(units):
            size = int(sum(_message_tokens(message) for message in unit) * self.calibration)
            if selected and used + size > budget:
                break
            selected = unit + selected
            used += size
        return selected
