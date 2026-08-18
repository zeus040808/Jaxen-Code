"""Agent control loop extracted from the runtime facade."""

import time

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .run_state import RunState, STOP_REASON_BLOCKED, STOP_REASON_REPEATED_FAILED_CALL
from . import tools as toolkit
from .workspace import clip, now


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

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
        agent.run_store.write_report(run_state, agent.redact_artifact(agent.build_report(run_state)))
        agent.emit_ui(
            "run_finished",
            status=run_state.status,
            stop_reason=run_state.stop_reason,
            final_answer=run_state.final_answer,
        )

    def _collect_background_notifications(self, run_state):
        agent = self.agent
        notifications = agent.background_tasks.collect()
        for notification in notifications:
            agent.task_state.record_background_notification(notification)
            agent.record({"role": "user", "content": notification, "created_at": now()})
            agent.emit_trace(run_state, "background_notification", {"content": clip(notification, 500)})
        return notifications

    def run(self, user_message):
        agent = self.agent
        run_started_at = time.monotonic()
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
        agent.memory.set_task_summary(user_message)
        agent.final_only_mode = bool(resume_partial)

        # record将用户消息保存在history
        agent.record({"role": "user", "content": user_message, "created_at": now()})

        # 创建一个run状态（每轮对话）
        run_state = RunState.create(run_id=agent.new_run_id(), request_id=agent.new_request_id(), user_request=user_message)
        agent.task_state.begin(user_message, run_state.run_id)
        agent.source_evidence.begin(agent.task_state.to_dict().get("task_id", ""))
        agent.session["source_evidence"] = agent.source_evidence.to_dict()
        agent.session["task_state"] = agent.task_state.to_dict()
        agent.session_path = agent.session_store.save(agent.session)
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
            },
        )
        agent.emit_ui("run_started", request=user_message)
        self._collect_background_notifications(run_state)
        for job in agent.cron_scheduler.consume():
            scheduled_message = f"[Scheduled] {job.prompt}"
            # 保存定时任务消息
            agent.record({"role": "user", "content": scheduled_message, "created_at": now()})
            agent.emit_trace(run_state, "cron_delivered", {"cron_id": job.id, "prompt": clip(job.prompt, 500)})

        tool_steps = 0
        attempts = 0
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
        truncation_recoveries = 0
        protocol_recoveries = 0

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / run_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while attempts < max_attempts:
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
                    f"tools {tool_steps}/{agent.max_steps}{mode}"
                ),
            )
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
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
                checkpoint = agent.create_checkpoint(run_state, user_message, trigger="freshness_mismatch")
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
                checkpoint = agent.create_checkpoint(run_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_run_state(run_state)
                agent.emit_trace(
                    run_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            # 局部分区按固定上限裁切是每轮都可能发生的常规整理，trace 中保留即可。
            # 只有 prompt 总量超出全局预算、不得不再次淘汰上下文时，才创建恢复检查点。
            # 否则一个普通问题的每次格式重试都会制造近乎相同的 checkpoint。
            material_reductions = [
                item
                for item in prompt_metadata.get("budget_reductions", [])
                if item.get("reason") == "total_budget"
            ]
            if material_reductions:
                checkpoint = agent.create_checkpoint(run_state, user_message, trigger="context_reduction")
                agent.run_store.write_run_state(run_state)
                agent.emit_trace(
                    run_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            agent.emit_trace(
                run_state,
                "model_requested",
                {
                    "attempts": run_state.attempts,
                    "tool_steps": run_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(agent.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                # 缓存策略
                prompt_cache_retention = "in_memory"
            model_started_at = time.monotonic()

            try:
                # 向模型发送请求，得到回答
                if hasattr(agent.model_client, "complete_stream"):
                    provider_kwargs = {}
                    if bool(getattr(agent.model_client, "supports_native_tools", False)):
                        provider_kwargs = agent.context_manager.provider_request(user_message)
                    raw = agent.model_client.complete_stream(
                        prompt,
                        agent.max_new_tokens,
                        prompt_cache_key=prompt_cache_key,
                        prompt_cache_retention=prompt_cache_retention,
                        event_callback=lambda event: self._provider_event(run_state, event),
                        tools=(
                            None
                            if agent.final_only_mode
                            else toolkit.provider_tool_schemas(agent.tools)
                        ),
                        **provider_kwargs,
                    )
                else:
                    raw = agent.model_client.complete(
                        prompt,
                        agent.max_new_tokens,
                        prompt_cache_key=prompt_cache_key,
                        prompt_cache_retention=prompt_cache_retention,
                    )
                completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
                native_assistant_message = dict(
                    getattr(agent.model_client, "last_native_assistant_message", {}) or {}
                )
                if completion_metadata:
                    # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                    # 方便统一写入 report 和 trace。
                    prompt_metadata.update(completion_metadata)
                agent.last_completion_metadata = completion_metadata
                agent.last_prompt_metadata = prompt_metadata

                # `finish_reason=length` 是不完整输出，不能交给宽松文本解析器
                # 误判成成功答案。主循环会在下方走一次无工具续写恢复。
                if completion_metadata.get("output_truncated"):
                    kind, payload = "truncated", raw
                else:
                    kind, payload = agent.parse(raw)
            except KeyboardInterrupt:
                final = "Interrupted by user. The run was stopped cleanly and can be resumed from its checkpoint."
                run_state.stop_interrupted(final)
                agent.task_state.mark_paused(run_state.stop_reason, "Resume the task when ready.")
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                agent.emit_trace(run_state, "run_interrupted", {"attempt": attempts, "tool_steps": tool_steps})
                self._finalize_run(
                    run_state,
                    user_message,
                    trigger=run_state.stop_reason,
                    run_started_at=run_started_at,
                )
                raise
            except Exception as exc:
                error_detail = agent.redact_text(str(exc))
                final = f"Model request failed: {error_detail}"
                run_state.stop_model_error(final)
                agent.task_state.mark_paused(run_state.stop_reason, "Retry the model request or resume this task.")
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
                self._finalize_run(
                    run_state,
                    user_message,
                    trigger=run_state.stop_reason,
                    run_started_at=run_started_at,
                )
                raise RuntimeError(final) from exc
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
                    "original_request": str(user_message),
                    "partial_answer": partial,
                    "run_id": run_state.run_id,
                    "created_at": now(),
                }
                run_state.stop_output_truncated(partial_answer=partial, final_answer=final)
                agent.task_state.mark_paused(
                    run_state.stop_reason,
                    "Retry with a narrower request or increase --max-new-tokens.",
                )
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                self._finalize_run(
                    run_state,
                    user_message,
                    trigger=run_state.stop_reason,
                    run_started_at=run_started_at,
                )
                return final

            if kind == "tool":
                if agent.final_only_mode or tool_steps >= agent.max_steps:
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
                    agent.task_state.mark_paused(run_state.stop_reason, "Resume from the latest checkpoint and continue the task.")
                    agent.record({"role": "assistant", "content": final, "created_at": now()})
                    self._finalize_run(
                        run_state,
                        user_message,
                        trigger=run_state.stop_reason,
                        run_started_at=run_started_at,
                    )
                    return final
                name = payload.get("name", "")
                name = agent.canonical_tool_name(name)
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
                checkpoint = agent.create_checkpoint(run_state, user_message, trigger="tool_executed")
                agent.run_store.write_run_state(run_state)
                agent.emit_trace(
                    run_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "tool_executed",
                    },
                )
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
                        f"Blocked safely: {result}\n"
                        f"Next step: {tool_result.metadata.get('recommended_action', 'Choose a permitted action.')}"
                    )
                    run_state.finish_blocked(final, stop_reason=stop_reason)
                    agent.task_state.mark_blocked(final)
                    agent.record({"role": "assistant", "content": final, "created_at": now()})
                    self._finalize_run(
                        run_state,
                        user_message,
                        trigger=run_state.stop_reason,
                        run_started_at=run_started_at,
                    )
                    return final
                continue

            if kind == "needs_user_input":
                final = str(payload).strip()
                agent.emit_ui("needs_user_input", question=final)
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                run_state.wait_for_user(final)
                agent.task_state.mark_waiting(final)
                self._finalize_run(
                    run_state,
                    user_message,
                    trigger=run_state.stop_reason,
                    run_started_at=run_started_at,
                )
                return final

            if kind == "blocked":
                final = str(payload).strip()
                agent.emit_ui("blocked", reason=final)
                agent.record({"role": "assistant", "content": final, "created_at": now()})
                run_state.finish_blocked(final)
                agent.task_state.mark_blocked(final)
                self._finalize_run(
                    run_state,
                    user_message,
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
                    agent.task_state.mark_paused(
                        run_state.stop_reason,
                        "Retry the request or switch models.",
                    )
                    agent.record({"role": "assistant", "content": final, "created_at": now()})
                    self._finalize_run(
                        run_state,
                        user_message,
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
                notice = (
                    "Runtime delivery check rejected the final answer because: "
                    + " | ".join(blockers[:6])
                    + ". Resolve the blockers, use <needs_user_input>, or use <blocked>."
                )
                agent.record({"role": "assistant", "content": notice, "created_at": now()})
                agent.emit_trace(run_state, "completion_deferred", {"blockers": blockers})
                if agent.background_tasks.pending_count():
                    time.sleep(0.05)
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
            # 保存模型最终回答
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            run_state.finish_success(final)
            agent.task_state.mark_completed(final)
            agent.auto_capture_durable_memory(user_message)
            agent.auto_deposit_skill(user_message, final, run_state)
            self._finalize_run(run_state, user_message, trigger="run_finished", run_started_at=run_started_at)
            return final

        # 达到限制时：
        final = "Stopped after too many malformed model responses without a valid tool call or final answer."
        run_state.stop_retry_limit(final)
        agent.task_state.mark_paused(run_state.stop_reason, "Return a valid tool call or resume with a clearer request.")
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        self._finalize_run(
            run_state,
            user_message,
            trigger=run_state.stop_reason or "run_stopped",
            run_started_at=run_started_at,
        )
        return final

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
