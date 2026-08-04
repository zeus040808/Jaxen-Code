"""Agent control loop extracted from the runtime facade."""

import time

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .run_state import RunState
from .workspace import clip, now


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

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

    def run(self, user_message):
        agent = self.agent
        run_started_at = time.monotonic()
        agent.last_prompt_metadata = {}
        agent.last_completion_metadata = {}
        agent.last_durable_promotions = []
        agent.last_durable_rejections = []
        agent.last_durable_superseded = []
        agent.memory.set_task_summary(user_message)

        # record将用户消息保存在history
        agent.record({"role": "user", "content": user_message, "created_at": now()})

        # 创建一个run状态（每轮对话）
        run_state = RunState.create(run_id=agent.new_run_id(), request_id=agent.new_request_id(), user_request=user_message)
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
        for notification in agent.background_tasks.collect():
            # 保存后台任务通知
            agent.record({"role": "user", "content": notification, "created_at": now()})
            agent.emit_trace(run_state, "background_notification", {"content": clip(notification, 500)})
        for job in agent.cron_scheduler.consume():
            scheduled_message = f"[Scheduled] {job.prompt}"
            # 保存定时任务消息
            agent.record({"role": "user", "content": scheduled_message, "created_at": now()})
            agent.emit_trace(run_state, "cron_delivered", {"cron_id": job.id, "prompt": clip(job.prompt, 500)})

        tool_steps = 0
        attempts = 0
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / run_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while attempts < max_attempts:
            attempts += 1
            # 增加调用次数
            run_state.record_attempt()
            # 记录状态
            agent.run_store.write_run_state(run_state)

            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
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
            # 上下文发生裁切
            if prompt_metadata.get("budget_reductions"):
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
                raw = agent.model_client.complete(
                    prompt,
                    agent.max_new_tokens,
                    prompt_cache_key=prompt_cache_key,
                    prompt_cache_retention=prompt_cache_retention,
                )
                completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
                if completion_metadata:
                    # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                    # 方便统一写入 report 和 trace。
                    prompt_metadata.update(completion_metadata)
                agent.last_completion_metadata = completion_metadata
                agent.last_prompt_metadata = prompt_metadata

                # 解析返回
                kind, payload = agent.parse(raw)
            except Exception as exc:
                error_detail = agent.redact_text(str(exc))
                final = f"Model request failed: {error_detail}"
                run_state.stop_model_error(final)
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

            if kind == "tool":
                if tool_steps >= agent.max_steps:
                    final = "Stopped after reaching the tool step limit without a final answer."
                    run_state.stop_step_limit(final)
                    agent.record({"role": "assistant", "content": final, "created_at": now()})
                    agent.promote_durable_memory(user_message, final)
                    self._finalize_run(
                        run_state,
                        user_message,
                        trigger=run_state.stop_reason,
                        run_started_at=run_started_at,
                    )
                    return final
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                run_state.record_tool(name)
                tool_started_at = time.monotonic()
                tool_result = agent.execute_tool(name, args)
                result = tool_result.content
                # 保存工具执行结果
                agent.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": result,
                        "created_at": now(),
                    }
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
                continue

            if kind == "retry":
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
                agent.run_store.write_run_state(run_state)
                continue

            final = (payload or raw).strip()
            # 保存模型最终回答
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            run_state.finish_success(final)
            agent.promote_durable_memory(user_message, final)
            self._finalize_run(run_state, user_message, trigger="run_finished", run_started_at=run_started_at)
            return final

        # 达到限制时：
        final = "Stopped after too many malformed model responses without a valid tool call or final answer."
        run_state.stop_retry_limit(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.promote_durable_memory(user_message, final)
        self._finalize_run(
            run_state,
            user_message,
            trigger=run_state.stop_reason or "run_stopped",
            run_started_at=run_started_at,
        )
        return final
