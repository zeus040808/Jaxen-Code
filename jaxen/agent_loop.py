"""一次 ask() 的控制循环：驱动模型决策、工具调用和停止条件。

AgentLoop 不持有跨请求会话；它通过 CodingAgent 读取能力并回写状态。
"""

import functools
import hashlib
import time
from pathlib import Path

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .run_state import (
    RunState,
    STOP_REASON_BLOCKED,
    STOP_REASON_EPISODE_TIMEOUT,
    STOP_REASON_REPEATED_FAILED_CALL,
    STOP_REASON_INCOMPLETE_DELIVERY,
)
from . import tools as toolkit
from .progress import VERIFICATION_REPAIR_STEPS
from .workspace import clip, now

# 交付门禁只提醒一次，之后放行（未解决项照实写进答案）。
# 实测强制驳回最常见的原因是"改了代码但没跑通测试"，反复驳回会逼模型
# 自己写测试文件来过门禁，越出任务的改动范围。提醒一次足够让它补验证。
MAX_DELIVERY_DEFERRALS = 1
VERIFICATION_TOOLS = {"run_shell", "read_file", "read_files", "search", "patch_file", "write_file"}


@functools.lru_cache(maxsize=1)
def agent_source_hash():
    """agent 代码的内容哈希（jaxen 下全部 .py）。

    不用 git 提交号：评测时代码挂进容器，没有 .git；而且未提交的改动也要能区分开。
    """
    digest = hashlib.sha256()
    package = Path(__file__).resolve().parent
    for path in sorted(package.rglob("*.py")):
        digest.update(str(path.relative_to(package)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


class AgentLoop:
    """控制单次用户请求的生命周期；每次 CodingAgent.ask() 新建一个。"""

    def __init__(self, agent):
        self.agent = agent
        # 完整对话记录（transcript.jsonl）的增量状态：上一轮已经写过的消息和 system 的哈希
        self._sent_messages = []
        self._sent_system_hash = ""

    def _transcript_request(self, run_state, request):
        """把这一轮发给模型的完整内容写进 transcript.jsonl。

        历史只追加，所以每轮只写和上一轮不同的部分：kept_messages 条沿用上一轮，后面是 new_messages。
        压缩改写了前面的消息时，kept 会变小、改写后的消息重新写一遍。按这个规则能逐轮还原模型看到的全部内容
        （工具结果全文、注入的运行时提示、摘要），而文件大小随轮数线性增长，不是平方增长。
        """
        agent = self.agent
        messages = agent.redact_artifact(list(request.messages))
        kept = 0
        for old, new in zip(self._sent_messages, messages):
            if old != new:
                break
            kept += 1
        entry = {
            "kind": "request",
            "attempt": run_state.attempts,
            "kept_messages": kept,
            "new_messages": messages[kept:],
            "tools": [str((tool.get("function") or tool).get("name", "")) for tool in request.tools or []],
            "max_new_tokens": request.max_new_tokens,
        }
        system_hash = hashlib.sha256(request.system_prompt.encode("utf-8")).hexdigest()[:16]
        entry["system_hash"] = system_hash
        if system_hash != self._sent_system_hash:
            entry["system_prompt"] = agent.redact_text(request.system_prompt)
            self._sent_system_hash = system_hash
        self._sent_messages = messages
        agent.run_store.append_transcript(run_state, entry)

    def _transcript_response(self, run_state, raw, completion_metadata):
        agent = self.agent
        agent.run_store.append_transcript(run_state, {
            "kind": "response",
            "attempt": run_state.attempts,
            "raw": agent.redact_text(raw or ""),
            "reasoning_content": agent.redact_text(getattr(agent.model_client, "last_reasoning_content", "") or ""),
            "finish_reason": completion_metadata.get("finish_reason"),
            "output_tokens": completion_metadata.get("output_tokens"),
        })

    @staticmethod
    def _tool_limit(agent):
        # 只在代码已修改且尚未通过检查时开放修复余量。
        pending = agent.progress.to_dict().get("verification_pending", False)
        return agent.max_steps + (VERIFICATION_REPAIR_STEPS if pending else 0)

    @staticmethod
    def _partial_answer(agent, raw):
        text = str(raw or "").strip()
        if "<final>" in text:
            text = agent.extract(text, "final").strip()
        return text

    @staticmethod
    def _merge_continuation(draft, continuation):
        draft = str(draft or "")
        continuation = str(continuation or "")
        if not draft:
            return continuation.strip()
        if not continuation:
            return draft.strip()
        continuation = continuation.strip()
        if continuation.startswith(draft):
            return continuation
        if draft.endswith(("\n", " ")) or continuation.startswith(("\n", " ")):
            return draft + continuation
        if draft.endswith(("。", "！", "？", ".", "!", "?", ":", "：", ";", "；")):
            return draft + "\n" + continuation
        return draft + continuation

    @staticmethod
    def _is_continuation_request(user_message):
        normalized = " ".join(str(user_message or "").strip().casefold().split())
        return normalized in {
            "继续",
            "继续回答",
            "继续说",
            "接着",
            "接着说",
            "接着回答",
            "continue",
            "go on",
            "resume",
        }

    def _finalize_run(self, run_state, user_message, trigger, run_started_at):
        """Persist one terminal RunState in a consistent order."""
        agent = self.agent
        checkpoint = agent.create_checkpoint(run_state, user_message, trigger=trigger)
        run_state.touch()
        agent.run_store.write_run_state(run_state)
        agent.emit_trace(
            run_state,
            "checkpoint_created",
            {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": trigger},
        )
        agent.emit_trace(
            run_state,
            "run_finished",
            {
                "status": run_state.status,
                "stop_reason": run_state.stop_reason,
                "final_answer": clip(run_state.final_answer, 1000),
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.append_transcript(run_state, {
            "kind": "final",
            "status": run_state.status,
            "stop_reason": run_state.stop_reason,
            "final_answer": agent.redact_text(run_state.final_answer or ""),
        })
        agent.run_store.write_report(run_state, agent.redact_artifact(agent.build_report(run_state)))
        agent.emit_ui(
            "run_finished",
            status=run_state.status,
            stop_reason=run_state.stop_reason,
            final_answer=run_state.final_answer,
        )

    def _collect_background_notifications(self, run_state):
        agent = self.agent
        notifications = agent.background_jobs.collect()
        for notification in notifications:
            agent.progress.record_background_notification(notification)
            agent.record({"role": "user", "content": notification, "created_at": now()})
            agent.emit_trace(run_state, "background_notification", {"content": clip(notification, 500)})
        return notifications

    def _episode_timed_out(self, run_started_at):
        return (
            time.monotonic() - run_started_at
        ) * 1000 >= self.agent.episode_timeout_ms

    def _finish_episode_timeout(self, run_state, user_message, run_started_at):
        agent = self.agent
        final = (
            f"Episode wall-clock timeout exceeded ({agent.episode_timeout_ms} ms). "
            "The run stopped before another action could start."
        )
        run_state.stop(STOP_REASON_EPISODE_TIMEOUT, final_answer=final)
        agent.progress.mark_paused(
            run_state.stop_reason,
            "Resume with a larger episode timeout or a narrower request.",
        )
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.emit_ui(
            "error",
            error_type="episode_timeout",
            message=final,
        )
        self._finalize_run(
            run_state,
            user_message,
            trigger=run_state.stop_reason,
            run_started_at=run_started_at,
        )
        return final

    def run(self, user_message, responses=None, request_id=None):
        agent = self.agent
        run_started_at = time.monotonic()
        agent.run_deadline = run_started_at + agent.episode_timeout_ms / 1000  # 子 agent 只能用剩下的时间
        agent.last_prompt_metadata = {}
        agent.last_completion_metadata = {}
        pending_output = agent.session.get("pending_output", {})
        resume_partial = (
            isinstance(pending_output, dict)
            and bool(str(pending_output.get("partial_answer", "")).strip())
            and self._is_continuation_request(user_message)
        )
        if isinstance(pending_output, dict) and pending_output and not resume_partial:
            # A new task supersedes an unfinished answer. Keeping that stale
            # draft would make a later bare "continue" resume the wrong task.
            agent.session["pending_output"] = {}
        agent.output_recovery_draft = (
            str(pending_output.get("partial_answer", "")) if resume_partial else ""
        )
        agent.output_recovery_active = bool(resume_partial)
        agent.model_output_correction = ""
        agent.last_auto_skill = {"status": "idle"}
        agent.last_auto_memory = {"status": "idle"}
        agent.tool_executor.reset_run_state()
        agent.final_only_mode = bool(resume_partial)
        agent.max_new_tokens_override = 0  # 正文为空的截断会临时提高输出上限，只在本次运行内有效

        # 创建一个run状态（每轮对话）
        run_state = RunState.create(
            run_id=agent.new_run_id(),
            request_id=str(request_id or agent.new_request_id()),
            user_request=user_message,
        )
        run_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_run_state = run_state
        # 每次 ask() 都会生成一个 run 目录。
        agent.current_run_dir = agent.run_store.start_run(run_state)

        agent.emit_trace(
            run_state,
            "run_started",
            {
                "request_id": run_state.request_id,
                "user_request": clip(user_message, 300),
                # 0 = 主 agent，1 = 子 agent（调查/审查）。子 agent 的 run 和主 agent 存在同一个目录里，分析时靠它区分
                "depth": agent.depth,
                "parent_run_id": agent.parent_run_id or None,
                # 版本和配置：不同版本跑出的 trace 混在一起时，单看 trace 就能认出来
                "provider": type(agent.model_client).__name__,
                "model": str(getattr(agent.model_client, "model", "") or ""),
                "agent_version": agent_source_hash(),
                "config": {
                    "max_steps": agent.max_steps,
                    "max_new_tokens": agent.max_new_tokens,
                    "episode_timeout_ms": agent.episode_timeout_ms,
                    "review_mode": agent.review_mode,
                    "toolset": agent.toolset,
                },
            },
        )
        agent.emit_ui("run_started", request=user_message)

        pending_resolution = agent.resolve_pending_input(
            responses=responses,
            fallback_text=user_message,
        )
        resolution_status = pending_resolution.get("status")
        effective_user_message = str(pending_resolution.get("text", user_message))
        # 审查要对照用户的完整原始需求：审批续跑时下面会把 effective_user_message 换成"已批准"提示，
        # progress 里的 goal 又被截断到 1000 字，所以在这里留一份原文。
        review_request = effective_user_message or str(agent.progress.to_dict().get("goal", ""))
        if resolution_status == "approved":
            action = pending_resolution["action"]
            safe_args = agent.redact_artifact(action["args"])
            agent.emit_ui("tool_started", name=action["name"], args=safe_args, resumed=True)
            tool_started_at = time.monotonic()
            resumed_result = agent.execute_pending_action(
                action,
                pending_resolution["expected_fingerprint"],
            )
            if bool(resumed_result.metadata.get("effective_tool_step", True)):
                run_state.record_tool(action["name"])
            agent.source_evidence.record_action(
                action["name"],
                action["args"],
                status=resumed_result.metadata.get("tool_status", "unknown"),
            )
            agent.session["source_evidence"] = agent.source_evidence.to_dict()
            agent.emit_ui(
                "tool_finished",
                name=action["name"],
                result=clip(resumed_result.content, 1200),
                metadata=dict(resumed_result.metadata or {}),
                resumed=True,
            )
            agent.emit_trace(
                run_state,
                "tool_executed",
                {
                    "name": action["name"],
                    "args": safe_args,
                    "result": clip(resumed_result.content, 500),
                    "resumed_after_approval": True,
                    "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                    **dict(resumed_result.metadata or {}),
                },
            )
            effective_user_message = "Approval granted for the exact pending action. Continue from its result."
            agent.record(
                {
                    "role": "user",
                    "content": effective_user_message + "\n" + resumed_result.prompt_content(),
                    "created_at": now(),
                }
            )
        elif resolution_status == "denied":
            effective_user_message = "Approval denied for the pending action."
        elif resolution_status in {"waiting", "invalid"}:
            request = pending_resolution.get("input") or agent.session.get("pending_input", {})
            final = str(request.get("question", "Waiting for the requested user input."))
            if resolution_status == "invalid":
                final = "Pending approval could not be resumed safely: " + str(
                    pending_resolution.get("reason", "invalid pending state")
                )
            run_state.wait_for_user(final)
            agent.progress.mark_waiting(final)
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            agent.emit_ui(
                "needs_user_input",
                input_id=str(request.get("input_id", "")),
                question=final,
                input_kind=str(request.get("kind", "clarification")),
            )
            self._finalize_run(
                run_state,
                user_message,
                trigger=run_state.stop_reason,
                run_started_at=run_started_at,
            )
            return final

        agent.working_set.set_task_summary(effective_user_message)
        if resolution_status != "approved":
            agent.record({"role": "user", "content": effective_user_message, "created_at": now()})
        agent.progress.begin(effective_user_message, run_state.run_id)
        agent.source_evidence.begin(agent.progress.to_dict().get("goal_id", ""))
        agent.session["source_evidence"] = agent.source_evidence.to_dict()
        agent.session["progress"] = agent.progress.to_dict()
        agent.session_path = agent.session_store.save(agent.session)

        if resolution_status == "denied":
            final = "Approval denied. The pending action was not executed."
            run_state.finish_success(final)
            agent.progress.mark_completed(final)
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            agent.emit_ui("approval_resolved", decision="denied", executed=False)
            self._finalize_run(
                run_state,
                effective_user_message,
                trigger="approval_denied_by_user",
                run_started_at=run_started_at,
            )
            return final

        self._collect_background_notifications(run_state)

        tool_steps = int(run_state.tool_steps)
        attempts = 0
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
        truncation_recoveries = 0
        delivery_deferrals = 0
        unresolved_blockers = []
        reviewed = False  # 每次运行最多审查一轮
        protocol_recoveries = 0

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 然后进入下一轮，直到停机条件满足
        while attempts < max_attempts:
            if self._episode_timed_out(run_started_at):
                return self._finish_episode_timeout(
                    run_state, effective_user_message, run_started_at
                )
            # 显式后台任务完成后，在下一次模型决策前立即回填。
            self._collect_background_notifications(run_state)
            attempts += 1
            # 增加调用次数
            run_state.record_attempt()
            # 记录状态
            agent.run_store.write_run_state(run_state)

            prompt_started_at = time.monotonic()
            mode = " | final-only" if agent.final_only_mode else ""
            agent.emit_ui(
                "status",
                message=(
                    f"Thinking - decision {attempts}/{max_attempts} | "
                    f"tools {tool_steps}/{self._tool_limit(agent)}{mode}"
                ),
            )
            tools = None if agent.final_only_mode else toolkit.provider_tool_schemas(agent.tools)
            request, prompt_metadata = agent.build_model_request(effective_user_message, tools=tools)
            request.event_callback = lambda event: self._provider_event(run_state, event)
            # 更新元数据
            agent.last_prompt_metadata = prompt_metadata
            agent.emit_trace(
                run_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )

            # 关键文件发生变化时创建
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                # 创建checkpoint
                checkpoint = agent.create_checkpoint(run_state, effective_user_message, trigger="freshness_mismatch")
                agent.run_store.write_run_state(run_state)
                agent.emit_trace(
                    run_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            # 运行环境发生变化
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    run_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(run_state, effective_user_message, trigger="workspace_mismatch")
                agent.run_store.write_run_state(run_state)
                agent.emit_trace(
                    run_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            # 上下文被压缩（轻压缩、摘要或兜底裁剪）时留一个可审计的恢复检查点。
            compaction = prompt_metadata.get("compaction")
            if compaction:
                agent.emit_ui("compaction", **compaction)
                checkpoint = agent.create_checkpoint(run_state, effective_user_message, trigger="context_reduction")
                agent.run_store.write_run_state(run_state)
                agent.emit_trace(
                    run_state,
                    "checkpoint_created",
                    {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": "context_reduction", **compaction},
                )
            agent.emit_trace(
                run_state,
                "model_requested",
                {
                    "attempts": run_state.attempts,
                    "tool_steps": run_state.tool_steps,
                    "estimated_input_tokens": prompt_metadata.get("estimated_input_tokens"),
                },
            )
            model_started_at = time.monotonic()
            self._transcript_request(run_state, request)

            try:
                agent.model_client.last_reasoning_content = ""
                # 模型响应包装返回
                raw = agent.model_client.complete(request)
                completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
                self._transcript_response(run_state, raw, completion_metadata)
                native_assistant_message = dict(
                    getattr(agent.model_client, "last_native_assistant_message", {}) or {}
                )
                if completion_metadata:
                    # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                    # 方便统一写入 report 和 trace。
                    prompt_metadata.update(completion_metadata)
                agent.last_completion_metadata = completion_metadata
                agent.last_prompt_metadata = prompt_metadata
                agent.context_manager.observe_usage(completion_metadata)
                if completion_metadata:
                    agent.emit_ui(
                        "usage",
                        input_tokens=int(completion_metadata.get("input_tokens", 0) or 0),
                        output_tokens=int(completion_metadata.get("output_tokens", 0) or 0),
                        cache_read_tokens=int(completion_metadata.get("cached_tokens", 0) or 0),
                        cache_write_tokens=int(completion_metadata.get("cache_write_tokens", 0) or 0),
                        requests=1,
                    )

                # `finish_reason=length` 是不完整输出，不能交给宽松文本解析器
                # 误判成成功答案。主循环会在下方走一次无工具续写恢复。
                if completion_metadata.get("output_truncated"):
                    kind, payload = "truncated", raw
                else:
                    kind, payload = agent.parse(raw)
            except KeyboardInterrupt:
                final = "Interrupted by user. The run was stopped cleanly and can be resumed from its checkpoint."
                run_state.stop_interrupted(final)
                agent.progress.mark_paused(run_state.stop_reason, "Resume the task when ready.")
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                agent.emit_trace(run_state, "run_interrupted", {"attempt": attempts, "tool_steps": tool_steps})
                self._finalize_run(
                    run_state,
                    effective_user_message,
                    trigger=run_state.stop_reason,
                    run_started_at=run_started_at,
                )
                raise
            except Exception as exc:
                error_detail = agent.redact_text(str(exc))
                agent.run_store.append_transcript(run_state, {
                    "kind": "model_error", "attempt": run_state.attempts,
                    "error_type": exc.__class__.__name__, "error": error_detail,
                })
                final = f"Model request failed: {error_detail}"
                run_state.stop_model_error(final)
                agent.progress.mark_paused(run_state.stop_reason, "Retry the model request or resume this task.")
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                agent.emit_trace(
                    run_state,
                    "model_failed",
                    {
                        "error": error_detail,
                        "error_type": exc.__class__.__name__,
                        "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                    },
                )
                agent.emit_ui(
                    "error",
                    error_type=exc.__class__.__name__,
                    message=error_detail,
                )
                self._finalize_run(
                    run_state,
                    effective_user_message,
                    trigger=run_state.stop_reason,
                    run_started_at=run_started_at,
                )
                raise RuntimeError(final) from exc
            if self._episode_timed_out(run_started_at):
                return self._finish_episode_timeout(
                    run_state, effective_user_message, run_started_at
                )
            agent.emit_trace(
                run_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )
            decision = {"kind": kind}
            if kind == "tool" and isinstance(payload, dict):
                decision.update(
                    {
                        "name": agent.canonical_tool_name(payload.get("name", "")),
                        "args": agent.redact_artifact(payload.get("args", {})),
                    }
                )
            elif kind in {"final", "needs_user_input", "blocked", "retry"}:
                decision["text"] = clip(agent.redact_text(payload or raw), 1000)
            agent.emit_trace(run_state, "model_decision", decision)

            if kind == "truncated":
                partial_piece = self._partial_answer(agent, raw)
                # 正文一个字都没有就被截断：输出额度全花在思考（或半截的工具调用）上了，"接着写草稿"无从接起
                # （SWE-bench sympy-23413：连续两轮 8192 个输出 token 全是思考，续写恢复照样截断，整题失败）。
                # 改成提高输出上限、工具照常可用、重发这一轮；只提一次，按实际用量计费，上限高不额外花钱。
                current_limit = int(getattr(agent, "max_new_tokens_override", 0) or agent.max_new_tokens)
                model_limit = int(getattr(agent.model_client, "max_output_tokens", 0) or current_limit * 4)
                raised_limit = min(model_limit, current_limit * 4)
                already_raised = bool(getattr(agent, "max_new_tokens_override", 0))
                if (not partial_piece.strip() and not agent.output_recovery_active
                        and not already_raised and raised_limit > current_limit):
                    agent.max_new_tokens_override = raised_limit
                    max_attempts += 1
                    agent.emit_trace(
                        run_state,
                        "output_budget_raised",
                        {"from_tokens": current_limit, "to_tokens": raised_limit,
                         "finish_reason": completion_metadata.get("finish_reason")},
                    )
                    continue
                partial = self._merge_continuation(agent.output_recovery_draft, partial_piece)
                run_state.partial_answer = partial
                agent.run_store.write_run_state(run_state)
                agent.emit_trace(
                    run_state,
                    "model_output_truncated",
                    {
                        "finish_reason": completion_metadata.get("finish_reason"),
                        "partial_chars": len(partial),
                        "recovery_attempt": truncation_recoveries + 1,
                    },
                )
                if truncation_recoveries == 0:
                    truncation_recoveries = 1
                    agent.output_recovery_draft = partial
                    agent.output_recovery_active = True
                    agent.final_only_mode = True
                    # 截断恢复是平台补救，不应挤掉原本的模型决策额度。
                    max_attempts += 1
                    agent.emit_trace(
                        run_state,
                        "output_recovery_started",
                        {"partial_chars": len(partial), "tools_disabled": True},
                    )
                    continue

                final = (
                    "The model response reached the output-token limit twice and could not be "
                    "completed safely. The partial draft was preserved in this run's partial_answer. "
                    "Retry with a narrower request or increase --max-new-tokens."
                )
                agent.session["pending_output"] = {
                    "original_request": str(effective_user_message),
                    "partial_answer": partial,
                    "run_id": run_state.run_id,
                    "created_at": now(),
                }
                run_state.stop_output_truncated(partial_answer=partial, final_answer=final)
                agent.progress.mark_paused(
                    run_state.stop_reason,
                    "Retry with a narrower request or increase --max-new-tokens.",
                )
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                self._finalize_run(
                    run_state,
                    effective_user_message,
                    trigger=run_state.stop_reason,
                    run_started_at=run_started_at,
                )
                return final

            if kind == "tool_batch":
                actions = payload
                names = [agent.canonical_tool_name(action["name"]) for action in actions]
                remaining = agent.max_steps - tool_steps
                if agent.final_only_mode or remaining < len(actions):
                    agent.final_only_mode = True if remaining <= 0 else agent.final_only_mode
                    agent.model_output_correction = (
                        f"Only {max(0, remaining)} tool steps remain. "
                        "Use a smaller read batch or return a final answer."
                    )
                    continue
                if len(actions) > 4 or any(
                    name not in agent.tool_executor.PARALLEL_READ_TOOLS for name in names
                ):
                    # 含写入/shell 的批次不再整批拒绝（那样白白多一轮往返），
                    # 而是按模型给出的顺序逐个执行。
                    executed, final = self._run_sequential_batch(
                        run_state, actions, names, native_assistant_message,
                        effective_user_message, run_started_at,
                    )
                    tool_steps += executed
                    if final is not None:
                        return final
                    continue
                for action, name in zip(actions, names):
                    agent.emit_ui("tool_started", name=name, args=agent.redact_artifact(action["args"]))
                try:
                    results = agent.tool_executor.execute_read_batch(actions)
                except ValueError as exc:
                    agent.model_output_correction = f"Invalid read batch: {exc}. Correct the calls."
                    continue
                agent.record({
                    "role": "assistant",
                    "content": native_assistant_message.get("content") or "",
                    "tool_calls": [
                        {"id": action["id"], "name": name, "args": action["args"]}
                        for action, name in zip(actions, names)
                    ],
                    "reasoning_content": str(native_assistant_message.get("reasoning_content", "") or ""),
                    "created_at": now(),
                })
                for action, name, result in zip(actions, names, results):
                    tool_steps += 1
                    run_state.record_tool(name)
                    agent.record({
                        "role": "tool", "name": name, "args": action["args"],
                        "content": result.prompt_content(), "tool_call_id": action["id"],
                        "created_at": now(),
                    })
                    agent.source_evidence.record_action(
                        name, action["args"], status=result.metadata.get("tool_status", "unknown")
                    )
                    agent.emit_trace(run_state, "tool_executed", {
                        "name": name, "args": agent.redact_artifact(action["args"]),
                        "result": clip(result.content, 500), "parallel_batch": True,
                        **dict(result.metadata or {}),
                    })
                    agent.emit_ui("tool_finished", name=name, result=clip(result.content, 1200),
                                  metadata=dict(result.metadata or {}))
                agent.session["source_evidence"] = agent.source_evidence.to_dict()
                agent.run_store.write_run_state(run_state)
                agent.create_checkpoint(run_state, effective_user_message, trigger="tool_batch_executed")
                continue

            if kind == "tool":
                name = agent.canonical_tool_name(payload.get("name", ""))
                limit = self._tool_limit(agent)
                if (tool_steps >= agent.max_steps and tool_steps < limit
                        and name not in VERIFICATION_TOOLS):
                    agent.model_output_correction = (
                        "Only verification and repair tools remain: run_shell, read_file, "
                        "read_files, search, patch_file, write_file."
                    )
                    continue
                if agent.final_only_mode or tool_steps >= limit:
                    if not agent.final_only_mode:
                        agent.final_only_mode = True
                        notice = (
                            "Tool budget exhausted. No further tools will execute. "
                            "Use the verified evidence and return final, needs_user_input, or blocked now."
                        )
                        agent.record({"role": "assistant", "content": notice, "created_at": now()})
                        agent.emit_trace(
                            run_state,
                            "final_only_mode_entered",
                            {"attempt": attempts, "tool_steps": tool_steps},
                        )
                        continue
                    final = "Stopped after the final-only decision still requested a tool."
                    run_state.stop_step_limit(final)
                    agent.progress.mark_paused(run_state.stop_reason, "Resume from the latest checkpoint and continue the task.")
                    agent.record({"role": "assistant", "content": final, "created_at": now()})
                    self._finalize_run(
                        run_state,
                        effective_user_message,
                        trigger=run_state.stop_reason,
                        run_started_at=run_started_at,
                    )
                    return final
                args = payload.get("args", {})
                agent.emit_ui("tool_started", name=name, args=args)
                tool_call_id = str(native_assistant_message.get("tool_call_id", "")).strip()
                if not tool_call_id:
                    tool_call_id = "call_" + agent.new_request_id().rsplit("-", 1)[-1]
                assistant_tool_item = {
                    "role": "assistant",
                    "content": native_assistant_message.get("content") or "",
                    "tool_call": {"id": tool_call_id, "name": name, "args": args},
                    "created_at": now(),
                }
                reasoning_content = str(
                    native_assistant_message.get("reasoning_content", "") or ""
                )
                if reasoning_content:
                    # Protocol-only state: Session keeps it so DeepSeek can
                    # continue a thinking-mode tool turn. UI and trace omit it.
                    assistant_tool_item["reasoning_content"] = reasoning_content
                agent.record(
                    assistant_tool_item
                )
                tool_started_at = time.monotonic()
                # 调用工具
                tool_result = agent.execute_tool(name, args)
                effective_step = bool(tool_result.metadata.get("effective_tool_step", True))
                if effective_step:
                    tool_steps += 1
                    run_state.record_tool(name)
                else:
                    agent.emit_trace(run_state, "tool_cache_hit", {"name": name, "args": args})
                # 提取工具调用结果，无论失败与否都写入记录
                result = tool_result.content
                agent.emit_ui(
                    "tool_finished",
                    name=name,
                    result=clip(result, 1200),
                    metadata=dict(tool_result.metadata or {}),
                )
                prompt_result = tool_result.prompt_content()
                # 保存工具执行结果
                agent.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": prompt_result,
                        "tool_call_id": tool_call_id,
                        "created_at": now(),
                    }
                )
                agent.source_evidence.record_action(
                    name,
                    args,
                    status=("cached" if tool_result.metadata.get("cache_hit") else tool_result.metadata.get("tool_status", "unknown")),
                    cache_hit=bool(tool_result.metadata.get("cache_hit", False)),
                )
                agent.session["source_evidence"] = agent.source_evidence.to_dict()
                if int(tool_result.metadata.get("cache_hit_count", 0) or 0) >= 2:
                    agent.final_only_mode = True
                    agent.record(
                        {
                            "role": "assistant",
                            "content": (
                                "The same unchanged read was requested repeatedly. "
                                "Further tools are disabled; answer from verified evidence now."
                            ),
                            "created_at": now(),
                        }
                    )
                    agent.emit_trace(
                        run_state,
                        "final_only_mode_entered",
                        {"reason": "repeated_cached_read", "name": name, "args": args},
                    )
                # 记录状态
                agent.run_store.write_run_state(run_state)
                agent.emit_trace(
                    run_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(tool_result.metadata or {}),
                    },
                )
                # 工具执行完成
                checkpoint = agent.create_checkpoint(run_state, effective_user_message, trigger="tool_executed")
                agent.run_store.write_run_state(run_state)
                agent.emit_trace(
                    run_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "tool_executed",
                    },
                )
                final = self._terminal_after_tool(
                    run_state, name, tool_result, effective_user_message, run_started_at
                )
                if final is not None:
                    return final
                continue

            if kind == "needs_user_input":
                final = str(payload).strip()
                input_request = agent.request_clarification(final)
                agent.emit_ui(
                    "needs_user_input",
                    input_id=input_request["input_id"],
                    input_kind="clarification",
                    question=final,
                )
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                run_state.wait_for_user(final)
                agent.progress.mark_waiting(final)
                self._finalize_run(
                    run_state,
                    effective_user_message,
                    trigger=run_state.stop_reason,
                    run_started_at=run_started_at,
                )
                return final

            if kind == "blocked":
                final = str(payload).strip()
                agent.emit_ui("blocked", reason=final)
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                run_state.finish_blocked(final)
                agent.progress.mark_blocked(final)
                self._finalize_run(
                    run_state,
                    effective_user_message,
                    trigger=run_state.stop_reason,
                    run_started_at=run_started_at,
                )
                return final

            if kind == "retry":
                if agent.looks_like_internal_tool_protocol(raw):
                    if protocol_recoveries == 0:
                        protocol_recoveries = 1
                        max_attempts += 1
                        agent.model_output_correction = (
                            "The previous response used an internal DSML tool protocol. Never expose "
                            "provider control syntax. Use native tool_calls when tools are available; "
                            "when tools are disabled, return a plain user-facing final, "
                            "needs_user_input, or blocked response."
                        )
                        agent.emit_trace(
                            run_state,
                            "internal_tool_protocol_rejected",
                            {"recovery_attempt": protocol_recoveries},
                        )
                        continue
                    final = (
                        "The model repeatedly returned an internal tool-call protocol instead of a "
                        "user-facing answer. The protocol was blocked and was not delivered."
                    )
                    run_state.stop_model_protocol_error(final)
                    agent.progress.mark_paused(
                        run_state.stop_reason,
                        "Retry the request or switch models.",
                    )
                    agent.record({"role": "assistant", "content": final, "created_at": now()})
                    self._finalize_run(
                        run_state,
                        effective_user_message,
                        trigger=run_state.stop_reason,
                        run_started_at=run_started_at,
                    )
                    return final
                # Parser feedback is a runtime instruction, not an assistant
                # answer. Persisting it as role=assistant poisons native chat
                # history and can make the next model turn continue the notice.
                agent.model_output_correction = str(payload)
                agent.emit_trace(
                    run_state,
                    "model_output_rejected",
                    {"correction": clip(agent.redact_text(payload), 1000)},
                )
                agent.run_store.write_run_state(run_state)
                continue

            blockers = agent.delivery_blockers()
            if blockers:
                # 门禁只有在模型「还能动手解决」时才值得驳回。工具已被禁用
                # （步数耗尽或进入 final-only）却仍要求先解决工具失败，就是死锁：
                # 解决需要工具，而工具不会再给了 —— 模型只能反复交同一份答案，
                # 直到 episode 超时。这条路径实测吃掉了 47% 的模型请求。
                actionable = not agent.final_only_mode and tool_steps < self._tool_limit(agent)
                delivery_deferrals += 1
                if actionable and delivery_deferrals <= MAX_DELIVERY_DEFERRALS:
                    notice = (
                        "Runtime delivery check (one-time reminder): before finishing, these items are open: "
                        + " | ".join(blockers[:6])
                        + ". Resolve them with the existing tools if you can. If you cannot, "
                        "return your final answer and state plainly what is unresolved."
                    )
                    agent.record({"role": "assistant", "content": notice, "created_at": now()})
                    agent.emit_trace(
                        run_state,
                        "completion_deferred",
                        {"blockers": blockers, "attempt": delivery_deferrals},
                    )
                    if agent.background_jobs.pending_count():
                        time.sleep(0.05)
                    continue

                # 不再驳回，但也不假装干净：把未解决项挂在答案里交出去，
                # 让下游判分和用户都看得见，而不是让它们消失在超时里。
                unresolved_blockers = list(blockers)
                agent.emit_trace(
                    run_state,
                    "completion_forced",
                    {
                        "blockers": blockers,
                        "reason": "tools_exhausted" if not actionable else "deferral_budget_spent",
                        "deferrals": delivery_deferrals,
                    },
                )

            # 交付前的独立审查（review.py）：改过代码、还能动手时，派只读子 agent 按清单复查一轮。
            # 有问题就把意见交回主 agent 继续改；它可以不采纳，但要在答案里说明理由。
            if (agent.review_mode != "off" and not reviewed and not unresolved_blockers
                    and not agent.final_only_mode and tool_steps < self._tool_limit(agent)):
                from .review import feedback_notice, needs_review, run_review, workspace_diff

                diff = workspace_diff(agent.root)
                if needs_review(agent.review_mode, diff):
                    reviewed = True
                    agent.emit_ui("tool_started", name="code_review", args={"diff_lines": diff.count("\n")})
                    review = run_review(agent, review_request, diff)
                    agent.emit_ui(
                        "tool_finished", name="code_review",
                        result=f"verdict: {review['verdict']}\n" + "\n".join(f"- {i}" for i in review["issues"]),
                        metadata={"tool_status": "ok"},
                    )
                    agent.emit_trace(run_state, "review_completed", {
                        "mode": agent.review_mode, "verdict": review["verdict"], "issues": review["issues"],
                        "parsed": review["parsed"], "status": review["status"], "usage": review["usage"],
                    })
                    if review["verdict"] == "issues":
                        agent.record({"role": "assistant", "content": feedback_notice(review), "created_at": now()})
                        continue

            final = (payload or raw).strip()
            if agent.output_recovery_active:
                final = self._merge_continuation(agent.output_recovery_draft, final)
                agent.emit_trace(
                    run_state,
                    "output_recovery_completed",
                    {"final_chars": len(final)},
                )
                agent.output_recovery_active = False
                agent.output_recovery_draft = ""
                agent.session["pending_output"] = {}
            if unresolved_blockers:
                final = (
                    final
                    + "\n\nUnresolved at delivery:\n"
                    + "\n".join(f"- {item}" for item in unresolved_blockers[:6])
                )
            # 保存模型最终回答
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            if unresolved_blockers:
                run_state.stop(STOP_REASON_INCOMPLETE_DELIVERY, final_answer=final)
                agent.progress.mark_paused(
                    STOP_REASON_INCOMPLETE_DELIVERY,
                    "Resolve the listed blockers before marking this task complete.",
                )
            else:
                run_state.finish_success(final)
                agent.progress.mark_completed(final)
                agent.auto_capture_long_term_memory(effective_user_message)
                agent.auto_deposit_skill(effective_user_message, final, run_state)
            self._finalize_run(run_state, effective_user_message, trigger="run_finished", run_started_at=run_started_at)
            return final

        # 达到限制时：
        final = "Stopped after too many malformed model responses without a valid tool call or final answer."
        run_state.stop_retry_limit(final)
        agent.progress.mark_paused(run_state.stop_reason, "Return a valid tool call or resume with a clearer request.")
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        self._finalize_run(
            run_state,
            effective_user_message,
            trigger=run_state.stop_reason or "run_stopped",
            run_started_at=run_started_at,
        )
        return final

    def _terminal_after_tool(self, run_state, name, tool_result, user_message, run_started_at):
        """Finish the run when a tool waits for approval or must stop it; else None."""
        agent = self.agent
        if tool_result.metadata.get("tool_status") == "waiting":
            input_request = dict(tool_result.metadata.get("input_request") or {})
            final = str(
                input_request.get("question")
                or f"Approval is required before {name} can execute."
            )
            run_state.wait_for_user(final)
            agent.progress.mark_waiting(final)
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            self._finalize_run(
                run_state, user_message, trigger="approval_requested", run_started_at=run_started_at,
            )
            return final
        if tool_result.metadata.get("stop_run"):
            error_code = str(tool_result.metadata.get("tool_error_code", "") or STOP_REASON_BLOCKED)
            security_event = str(tool_result.metadata.get("security_event_type", ""))
            if error_code == "repeated_failed_call":
                stop_reason = STOP_REASON_REPEATED_FAILED_CALL
            elif security_event in {"approval_denied", "read_only_block", "path_escape"}:
                stop_reason = security_event
            else:
                stop_reason = error_code
            final = (
                f"Blocked safely: {tool_result.content}\n"
                f"Next step: {tool_result.metadata.get('recommended_action', 'Choose a permitted action.')}"
            )
            run_state.finish_blocked(final, stop_reason=stop_reason)
            agent.progress.mark_blocked(final)
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            self._finalize_run(
                run_state, user_message, trigger=run_state.stop_reason, run_started_at=run_started_at,
            )
            return final
        return None

    def _run_sequential_batch(self, run_state, actions, names, native_message, user_message, run_started_at):
        """Run a batch one call at a time, in the order the model returned it.

        Returns (effective tool steps used, terminal answer or None). When a call
        waits for approval or stops the run, later calls are not executed but
        still get a tool result, so every assistant tool_call keeps its pair.
        """
        agent = self.agent
        agent.record({
            "role": "assistant",
            "content": native_message.get("content") or "",
            "tool_calls": [
                {"id": action["id"], "name": name, "args": action["args"]}
                for action, name in zip(actions, names)
            ],
            "reasoning_content": str(native_message.get("reasoning_content", "") or ""),
            "created_at": now(),
        })
        executed = 0
        stopping = None  # (name, tool_result) of the call that ends the run
        for action, name in zip(actions, names):
            args = action["args"]
            if stopping is not None:
                agent.record({
                    "role": "tool", "name": name, "args": args, "tool_call_id": action["id"],
                    "content": f"not executed: earlier call {stopping[0]} in this batch ended the step",
                    "created_at": now(),
                })
                continue
            agent.emit_ui("tool_started", name=name, args=agent.redact_artifact(args))
            started_at = time.monotonic()
            tool_result = agent.execute_tool(name, args)
            if bool(tool_result.metadata.get("effective_tool_step", True)):
                executed += 1
                run_state.record_tool(name)
            agent.record({
                "role": "tool", "name": name, "args": args, "tool_call_id": action["id"],
                "content": tool_result.prompt_content(), "created_at": now(),
            })
            agent.source_evidence.record_action(
                name, args, status=tool_result.metadata.get("tool_status", "unknown")
            )
            agent.emit_trace(run_state, "tool_executed", {
                "name": name, "args": agent.redact_artifact(args),
                "result": clip(tool_result.content, 500), "sequential_batch": True,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                **dict(tool_result.metadata or {}),
            })
            agent.emit_ui("tool_finished", name=name, result=clip(tool_result.content, 1200),
                          metadata=dict(tool_result.metadata or {}))
            if tool_result.metadata.get("tool_status") == "waiting" or tool_result.metadata.get("stop_run"):
                stopping = (name, tool_result)
        agent.session["source_evidence"] = agent.source_evidence.to_dict()
        agent.run_store.write_run_state(run_state)
        agent.create_checkpoint(run_state, user_message, trigger="tool_batch_executed")
        if stopping is None:
            return executed, None
        return executed, self._terminal_after_tool(
            run_state, stopping[0], stopping[1], user_message, run_started_at
        )

    def _provider_event(self, run_state, event):
        """Render provider progress and persist a compact diagnostic trace."""
        agent = self.agent
        event = dict(event or {})
        event_type = str(event.pop("type", "provider_event"))
        safe_payload = agent.redact_artifact(event)
        agent.emit_ui(event_type, **safe_payload)
        if event_type in {"provider_status", "reasoning_summary"}:
            trace_payload = {"provider_event_type": event_type, **safe_payload}
            if "text" in trace_payload:
                trace_payload["text"] = clip(trace_payload["text"], 1000)
            agent.emit_trace(run_state, "provider_event", trace_payload)
