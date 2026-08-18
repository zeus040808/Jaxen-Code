"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# 默认总预算
# section是上下文分区设计
DEFAULT_TOTAL_BUDGET = 24000

# 不同内容划分空间
DEFAULT_SECTION_BUDGETS = {
    "prefix": 5200,
    "working_memory": 850,
    "durable_memory": 500,
    "task_state": 950,
    "evidence": 5200,
    "skills": 1200,
    "history": 7200,
}

# 最低预算
DEFAULT_SECTION_FLOORS = {
    "prefix": 5000,
    "working_memory": 180,
    "durable_memory": 150,
    "task_state": 120,
    "evidence": 1800,
    "skills": 220,
    "history": 1500,
}
# 当 prompt 超预算时，会优先压缩这些 section。
# 超预算时的牺牲顺序
DEFAULT_REDUCTION_ORDER = (
    "skills",
    "durable_memory",
    "working_memory",
    "history",
    "task_state",
    "evidence",
    "prefix",
)

# 既是处理顺序，也是最终拼接顺序
SECTION_ORDER = (
    "prefix",
    "working_memory",
    "durable_memory",
    "task_state",
    "evidence",
    "skills",
    "history",
    "current_request",
)
CURRENT_REQUEST_SECTION = "current_request"

# 把文本限制在指定字符数以内
def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."

# 表示某一段上下文处理前后的状态
# SectionRender 是每个分区处理后的记录
@dataclass
class SectionRender:
    raw: str
    budget: int  # 允许使用的字符数
    rendered: str # 裁剪后真正进入 prompt 的内容
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


class ContextManager:
    # __init__ 是 Python 类里的初始化方法，当你创建对象的时候，它会自动执行
    # 创建时可以传入的参数
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        # 复制默认字典，避免修改某个 ContextManager 的预算时影响全局默认值
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        # 如果用户传参，则更新
        if section_budgets:
            provided = {str(key): int(value) for key, value in section_budgets.items()}
            # 旧调用者可继续传 memory 总预算；V2 按固定比例分给三个明确职责。
            if "memory" in provided:
                memory_budget = provided.pop("memory")
                self.section_budgets.update(
                    {
                        "working_memory": int(memory_budget * 0.4),
                        "durable_memory": int(memory_budget * 0.375),
                        "task_state": memory_budget - int(memory_budget * 0.4) - int(memory_budget * 0.375),
                    }
                )
            self.section_budgets.update(provided)
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        # 牺牲顺序
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)
        self._last_rendered = {}

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `HH.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        # 每一轮开始时计算每段最低能缩到多少
        self.section_floors = self._compute_section_floors()

        memory_enabled = True
        context_reduction_enabled = True
        # 可以独立关闭memory、相关记忆检索、上下文压缩
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")

        # 收集基础内容，只收集原材料，还没有生成 history，也没有裁剪
        section_texts = {
            # getattr：获取对象的某个属性，如果属性不存在，则返回默认值
            "prefix": str(getattr(self.agent, "prefix", "")),
            "working_memory": (
                "Working memory:\n- disabled"
                if not memory_enabled
                else str(self.agent.working_memory_text())
            ),
            "durable_memory": (
                "Curated durable memory:\n- disabled"
                if not memory_enabled
                else str(self.agent.durable_memory_text())
            ),
            # Task state 是执行控制状态，不随 memory feature flag 一起消失。
            "task_state": self._task_and_run_state_text(),
            "evidence": str(self.agent.evidence_text()),
            "skills": (
                "Available skills:\n- disabled"
                if not getattr(self.agent, "feature_enabled", lambda name: False)("skills")
                else str(self.agent.skills_text())
            ),
            "history": "",
            CURRENT_REQUEST_SECTION: self._current_request_text(user_message),
        }

        # 恢复检查点，则附加到 prefix
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            # Checkpoint is recovery-critical. Put it before the regular prefix
            # so section clipping cannot discard it from the tail.
            section_texts["prefix"] = checkpoint_text + "\n\n" + section_texts["prefix"]

        # Skill 正文不做自动召回：这里始终只注入目录，模型匹配后用
        # skill_view 显式加载正文，避免把全部流程塞进每一轮 prompt。
        selected_notes = []

        # 未开启上下文裁剪（用于测试）
        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
            )
            self._last_rendered = rendered
            return prompt, metadata

        budgets = dict(self.section_budgets)


        # 压缩后的section（正常局部分模块裁切）
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
        # 组装prompt
        prompt = self._assemble_prompt(rendered)
        # Record the first section-budget clipping as well as later global
        # reductions. This makes context reduction observable to checkpoints.
        reduction_log = [
            {
                "section": section,
                "before_chars": render.raw_chars,
                "after_chars": render.rendered_chars,
                "reason": "section_budget",
            }
            for section, render in rendered.items()
            if section != CURRENT_REQUEST_SECTION and render.rendered_chars < render.raw_chars
        ]

        # 如果 prompt 超预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先压缩 Skill 目录，再压缩 history，最后才动关键执行状态。
        # 最新用户请求永远不裁剪，因为那是本轮最重要的输入。

        # 整份 prompt 的全局兜底裁切
        while len(prompt) > self.total_budget:
            # 超出部分
            overflow = len(prompt) - self.total_budget
            reduced = False
            # 按照顺序裁切
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                # 裁切记录
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                        "reason": "total_budget",
                    }
                )
                budgets[section] = new_budget
                # 进入裁切
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        # 记录本轮 prompt 长度、每段裁剪情况、选中了哪些记忆、
        # 历史折叠了多少内容，用于 trace 和 report
        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=section_texts,
        )
        self._last_rendered = rendered
        return prompt, metadata

    def provider_request(self, user_message):
        """Return role-correct Chat Completions input for native-tool providers.

        ``build()`` remains the compatibility representation used by text-only
        providers, trace metadata and tests.  Native providers receive the same
        selected context, but rules live in ``system`` and recent tool exchanges
        retain their assistant/tool roles.
        """

        rendered = self._last_rendered
        if not rendered:
            self.build(user_message)
            rendered = self._last_rendered
        system_prompt = rendered["prefix"].rendered
        context_names = (
            "working_memory",
            "durable_memory",
            "task_state",
            "evidence",
            "skills",
        )
        context = "\n\n".join(rendered[name].rendered for name in context_names)
        if bool(getattr(self.agent, "output_recovery_active", False)):
            draft = _tail_clip(getattr(self.agent, "output_recovery_draft", ""), 6000)
            context += (
                "\n\nRuntime output recovery (authoritative): the previous model response "
                "reached its output-token limit. Return only the missing continuation as a final answer. "
                "Do not repeat the draft and do not request more tools."
            )
            if draft:
                context += "\nPartial draft to continue exactly:\n" + draft
        correction = str(getattr(self.agent, "model_output_correction", "") or "").strip()
        if correction:
            context += "\n\nRuntime format correction (authoritative): " + correction
        if bool(getattr(self.agent, "final_only_mode", False)):
            context += (
                "\n\nRuntime directive (authoritative): tools are disabled for this decision. "
                "Return final, needs_user_input, or blocked using existing evidence."
            )
        messages = [{"role": "user", "content": "Current task context:\n" + context}]
        messages.extend(self._native_history_messages())
        if not messages[1:]:
            messages.append({"role": "user", "content": str(user_message)})
        return {"system_prompt": system_prompt, "messages": messages}

    def _native_history_messages(self):
        """Convert recent Session items to valid role-aware chat messages."""

        history = list(getattr(self.agent, "session", {}).get("history", []))[-10:]
        messages = []
        known_tool_calls = set()
        for item in history:
            role = str(item.get("role", ""))
            if role == "assistant" and item.get("tool_call"):
                call = item["tool_call"]
                call_id = str(call.get("id", ""))
                if not call_id:
                    continue
                reasoning_content = str(item.get("reasoning_content", "") or "")
                if (
                    bool(
                        getattr(
                            self.agent.model_client,
                            "requires_reasoning_content_for_tool_calls",
                            False,
                        )
                    )
                    and not reasoning_content
                ):
                    # Sessions written before thinking-mode protocol support
                    # cannot reconstruct discarded reasoning. Do not replay an
                    # invalid assistant tool call: its following tool result is
                    # safely converted to a legacy user observation below.
                    continue
                known_tool_calls.add(call_id)
                assistant_message = {
                    "role": "assistant",
                    "content": str(item.get("content", "")).strip() or None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": str(call.get("name", "")),
                                "arguments": json.dumps(
                                    call.get("args", {}), ensure_ascii=False, sort_keys=True
                                ),
                            },
                        }
                    ],
                }
                if reasoning_content:
                    # Never summarize or clip this field: DeepSeek thinking
                    # mode requires the exact value beside the tool call.
                    assistant_message["reasoning_content"] = reasoning_content
                messages.append(assistant_message)
                continue
            if role == "tool":
                call_id = str(item.get("tool_call_id", ""))
                if call_id and call_id in known_tool_calls:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": _tail_clip(item.get("content", ""), 2600),
                        }
                    )
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Legacy tool observation [{item.get('name', '')}]:\n"
                                + _tail_clip(item.get("content", ""), 900)
                            ),
                        }
                    )
                continue
            if role in {"user", "assistant"}:
                content = str(item.get("content", "")).strip()
                if content:
                    messages.append({"role": role, "content": _tail_clip(content, 1600)})
        return self._budget_native_messages(messages, budget=7200)

    @staticmethod
    def _budget_native_messages(messages, budget):
        """Keep recent native messages within budget without splitting tool pairs."""

        units = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if (
                message.get("role") == "assistant"
                and message.get("tool_calls")
                and index + 1 < len(messages)
                and messages[index + 1].get("role") == "tool"
            ):
                units.append([message, messages[index + 1]])
                index += 2
                continue
            units.append([message])
            index += 1

        selected = []
        used = 0
        for unit in reversed(units):
            size = len(json.dumps(unit, ensure_ascii=False, sort_keys=True))
            if selected and used + size > int(budget):
                break
            if size > int(budget) and not selected:
                # Per-message clipping normally prevents this; keep a compact
                # newest observation as a deterministic final fallback.
                compact = []
                for message in unit:
                    item = dict(message)
                    if isinstance(item.get("content"), str):
                        item["content"] = _tail_clip(item["content"], max(80, int(budget) // len(unit)))
                    compact.append(item)
                unit = compact
                size = len(json.dumps(unit, ensure_ascii=False, sort_keys=True))
            selected = unit + selected
            used += size
        return selected

    # 关闭压缩
    def _render_sections_without_reduction(self, section_texts, selected_notes=None):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "working_memory": SectionRender(raw=section_texts["working_memory"], budget=len(section_texts["working_memory"]), rendered=section_texts["working_memory"], details={}),
            "durable_memory": SectionRender(raw=section_texts["durable_memory"], budget=len(section_texts["durable_memory"]), rendered=section_texts["durable_memory"], details={}),
            "task_state": SectionRender(raw=section_texts["task_state"], budget=len(section_texts["task_state"]), rendered=section_texts["task_state"], details={}),
            "evidence": SectionRender(raw=section_texts["evidence"], budget=len(section_texts["evidence"]), rendered=section_texts["evidence"], details={}),
            "skills": SectionRender(raw=section_texts["skills"], budget=len(section_texts["skills"]), rendered=section_texts["skills"], details={}),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    # 设置最低预算为1/4
    def _compute_section_floors(self):
        self._normalize_legacy_memory_budget()
        floors = {
            section: max(20, int(budget) // 4)
            for section, budget in self.section_budgets.items()
        }
        floors.update(self._section_floor_overrides)
        # Everything before the workspace marker is an execution contract:
        # identity, rules and the complete tool catalog. It must never vanish.
        marker = "\n\nWorkspace snapshot (compressible):\n"
        prefix = str(getattr(self.agent, "prefix", ""))
        if marker in prefix:
            floors["prefix"] = max(floors.get("prefix", 0), prefix.index(marker) + len(marker))
        return floors

    def _normalize_legacy_memory_budget(self):
        """兼容测试/配置在构造后直接写入 section_budgets['memory']。"""

        if "memory" not in self.section_budgets:
            return
        memory_budget = int(self.section_budgets.pop("memory"))
        working = int(memory_budget * 0.4)
        durable = int(memory_budget * 0.375)
        self.section_budgets.update(
            {
                "working_memory": working,
                "durable_memory": durable,
                "task_state": memory_budget - working - durable,
            }
        )

    # 裁切prompt
    def _render_sections(self, section_texts, budgets, selected_notes=None):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            # 完全不截断
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "evidence":
                raw = section_texts[section]
                ledger = getattr(self.agent, "source_evidence", None)
                rendered_text = ledger.render(int(budget or 0)) if ledger is not None else _tail_clip(raw, int(budget or 0))
                rendered[section] = SectionRender(
                    raw=raw,
                    budget=int(budget or 0),
                    rendered=rendered_text,
                    details={},
                )
            # 历史最复杂，需要去重、摘要和最近优先
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                raw = section_texts[section]
                if section == "prefix":
                    rendered_text = self._render_prefix(raw, int(budget))
                else:
                    rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    @staticmethod
    def _render_prefix(raw, budget):
        marker = "\n\nWorkspace snapshot (compressible):\n"
        if marker not in raw:
            return _tail_clip(raw, budget)
        critical, workspace = raw.split(marker, 1)
        protected = critical + marker
        if budget <= len(protected):
            # Prefer a bounded overflow over silently deleting tool contracts.
            return protected
        return protected + _tail_clip(workspace, budget - len(protected))

    # 历史的分层裁切
    def _render_history_section(self, budget):
        # history直接通过读取session获取
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)
        if not history:
            rendered = "Transcript:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                },
            )

        # 原始 transcript 只是短期上下文；代码事实由 Evidence Ledger 保底。
        # 因此只完整保留极少量最近消息，避免两个大 read 结果互相挤出。
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)
        history_entries, history_details = self._compressed_history_entries(history, recent_start)
        rendered_entries = []
        for entry in reversed(history_entries):
            recent = bool(entry.get("recent", False))
            candidate_lines = list(entry.get("lines", []))
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
            if len(candidate_rendered) <= budget:
                rendered_entries = candidate_entries
                continue
            if recent:
                available = budget - len("Transcript:")
                if rendered_entries:
                    available -= sum(len(line) + 1 for line in rendered_entries)
                available = max(20, available - 1)
                candidate_lines = [_tail_clip(line, available) for line in candidate_lines]
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
                if len(candidate_rendered) <= budget:
                    rendered_entries = candidate_entries
            else:
                smaller_lines = [_tail_clip(line, 20) for line in candidate_lines]
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["Transcript:", *smaller_entries])
                if len(smaller_rendered) <= budget:
                    rendered_entries = smaller_entries
        rendered = "\n".join(["Transcript:", *rendered_entries])

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
                **history_details,
            },
        )

    # 把每条历史变成摘要候选
    def _compressed_history_entries(self, history, recent_start):
        entries = []
        seen_older_reads = set()
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        for index, item in enumerate(history):
            recent = index >= recent_start
            if recent:
                if item.get("role") == "tool":
                    line_limit = 2600
                elif item.get("role") == "assistant" and item.get("tool_call"):
                    line_limit = 500
                else:
                    line_limit = 1200
                entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, line_limit),
                    }
                )
                continue

            if item["role"] == "tool" and item["name"] in {"read_file", "read_files"}:
                paths = (
                    [str(item["args"].get("path", "")).strip()]
                    if item["name"] == "read_file"
                    else [str(path).strip() for path in item["args"].get("paths", [])]
                )
                summaries = []
                for path in paths:
                    if not path or path in seen_older_reads:
                        details["collapsed_duplicate_reads"] += int(bool(path))
                        continue
                    seen_older_reads.add(path)
                    summary = self._reusable_file_summary(path)
                    if summary:
                        summaries.append(f"{path} -> {summary}")
                if summaries:
                    entries.append({"recent": False, "lines": summaries})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += len(summaries)
                    continue

            if item["role"] == "tool":
                summary_line = self._summarize_old_tool_item(item)
                entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue

            entries.append({"recent": False, "lines": self._render_history_item(item, 60)})

        return entries, details

    def _reusable_file_summary(self, path):
        evidence = getattr(self.agent, "source_evidence", None)
        if evidence is not None and hasattr(evidence, "file_summary"):
            return evidence.file_summary(path)
        return ""

    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()]
            summary = " | ".join(lines[:3]) if lines else "(empty)"
            return f"{command} -> {summary}"
        return self._render_history_item(item, 60)[0]

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = _tail_clip(item["content"], max(20, line_limit))
            return [prefix, content]
        if item.get("role") == "assistant" and item.get("tool_call"):
            call = item["tool_call"]
            return [
                f"[assistant/tool_call:{call.get('name', '')}] "
                + json.dumps(call.get("args", {}), ensure_ascii=False, sort_keys=True)
            ]
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]

    # 最终的拼接函数
    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["working_memory"].rendered,
                rendered["durable_memory"].rendered,
                rendered["task_state"].rendered,
                rendered["evidence"].rendered,
                rendered["skills"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, user_message, section_texts):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        # 兼容旧评估口径：memory 是三个独立 section 的聚合视图，不再是实际 section。
        memory_sections = ("working_memory", "durable_memory", "task_state")
        section_metadata["memory"] = {
            "raw_chars": sum(section_metadata[name]["raw_chars"] for name in memory_sections),
            "budget_chars": sum(section_metadata[name]["budget_chars"] for name in memory_sections),
            "rendered_chars": sum(section_metadata[name]["rendered_chars"] for name in memory_sections),
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "skills": {
                "raw_chars": rendered["skills"].raw_chars,
                "rendered_chars": rendered["skills"].rendered_chars,
                "available_count": len(getattr(getattr(self.agent, "skill_store", None), "list", lambda: [])()),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
            },
            "evidence": {
                "raw_chars": rendered["evidence"].raw_chars,
                "rendered_chars": rendered["evidence"].rendered_chars,
                "file_count": len(getattr(getattr(self.agent, "source_evidence", None), "to_dict", lambda: {"files": {}})().get("files", {})),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }

    def _current_request_text(self, user_message):
        lines = ["Current user request:", str(user_message)]
        if bool(getattr(self.agent, "output_recovery_active", False)):
            lines.extend(
                [
                    "",
                    "Runtime output recovery (authoritative):",
                    "- The previous response reached its output-token limit.",
                    "- Return only the missing continuation; do not repeat the draft or call tools.",
                    "- Partial draft:",
                    _tail_clip(getattr(self.agent, "output_recovery_draft", ""), 6000),
                ]
            )
        correction = str(getattr(self.agent, "model_output_correction", "") or "").strip()
        if correction:
            lines.extend(["", "Runtime format correction (authoritative):", correction])
        if bool(getattr(self.agent, "final_only_mode", False)):
            lines.extend(
                [
                    "",
                    "Runtime directive (authoritative):",
                    "- Tool execution is disabled for this decision.",
                    "- Use the verified evidence already supplied.",
                    "- Return exactly one <final>, <needs_user_input>, or <blocked> response now.",
                ]
            )
        return "\n".join(lines)

    def _task_and_run_state_text(self):
        lines = [str(self.agent.task_state_text())]
        run_state = getattr(self.agent, "current_run_state", None)
        if run_state is None:
            return "\n".join(lines)
        max_steps = int(getattr(self.agent, "max_steps", 0))
        max_attempts = max(max_steps * 3, max_steps + 4)
        lines.extend(
            [
                "Run progress (control data, not source evidence):",
                f"- decisions: {int(run_state.attempts)}/{max_attempts}",
                f"- effective_tools: {int(run_state.tool_steps)}/{max_steps}",
                f"- last_tool: {run_state.last_tool or '-'}",
                f"- final_only: {bool(getattr(self.agent, 'final_only_mode', False))}",
            ]
        )
        return "\n".join(lines)
