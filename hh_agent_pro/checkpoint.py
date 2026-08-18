"""Checkpoint and resume-state helpers.
    任务检查点、续跑书签、阶段性存档摘要
"""

import uuid

from .workspace import clip, file_freshness, now

# 所以 Checkpoint 把任务进度浓缩成：
# 目标：修复登录认证问题
# 已完成：已经定位 token 验证错误
# 阻塞：达到步骤上限
# 下一步：运行登录测试
# 关键文件：auth.py、user.py


# Checkpoint 存在 Session 里面，但 Session 不等于 Checkpoint
# 恢复状态
CHECKPOINT_SCHEMA_VERSION = "phase1-v1"
# 没有旧检查点，通常是新 Session。
CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_FULL_VALID_STATUS = "full-valid"
# 某些关键文件已经变化或删除
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
# 模型、工作目录、工具、配置或仓库指纹发生变化
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"
# Checkpoint 数据版本与当前代码不兼容
CHECKPOINT_SCHEMA_MISMATCH_STATUS = "schema-mismatch"
MAX_CHECKPOINTS = 100

RUNTIME_IDENTITY_KEYS = (
    "cwd",
    "model",
    "model_client",
    "approval_policy",
    "read_only",
    "max_steps",
    "max_new_tokens",
    "feature_flags",
    "shell_env_allowlist",
    "workspace_fingerprint",
    "tool_signature",
)

# 记录创建时的模型、工作区、工具等运行环境（runtime——identity）
def current_runtime_identity(agent):
    return {
        "session_id": agent.session.get("id", ""),
        "cwd": str(agent.root),
        "model": str(getattr(agent.model_client, "model", "")),
        "model_client": agent.model_client.__class__.__name__,
        "approval_policy": agent.approval_policy,
        "read_only": bool(agent.read_only),
        "max_steps": int(agent.max_steps),
        "max_new_tokens": int(agent.max_new_tokens),
        "feature_flags": dict(agent.feature_flags),
        "shell_env_allowlist": list(agent.shell_env_allowlist),
        "workspace_fingerprint": getattr(getattr(agent, "prefix_state", None), "workspace_fingerprint", agent.workspace.fingerprint()),
        "tool_signature": agent.tool_signature(),
    }

# 定义 Checkpoint 状态
def checkpoint_state(agent):
    agent._ensure_session_shape()
    return agent.session["checkpoints"]

# 获取当前 Checkpoint
def current_checkpoint(agent):
    state = checkpoint_state(agent)
    checkpoint_id = str(state.get("current_id", "")).strip()
    if not checkpoint_id:
        return None
    return state.get("items", {}).get(checkpoint_id)

# 恢复检查
def evaluate_resume_state(agent):
    previous_resume_state = dict(agent.session.get("resume_state", {}) or {})
    invalidated = agent.invalidate_stale_memory()
    checkpoint = current_checkpoint(agent)
    status = CHECKPOINT_NONE_STATUS
    stale_paths = list(invalidated)
    mismatch_fields = []
    if checkpoint:
        # 版本不一致
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            status = CHECKPOINT_SCHEMA_MISMATCH_STATUS
        else:
            for item in checkpoint.get("key_files", []):
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                expected = item.get("freshness")
                current = file_freshness(path, agent.root)
                if expected != current and path not in stale_paths:
                    stale_paths.append(path)
            saved_identity = dict(checkpoint.get("runtime_identity", {}) or agent.session.get("runtime_identity", {}) or {})
            current_identity = current_runtime_identity(agent)
            for key in RUNTIME_IDENTITY_KEYS:
                if key not in saved_identity:
                    continue
                if saved_identity.get(key) != current_identity.get(key):
                    mismatch_fields.append(key)
            mismatch_fields.sort()
            # 文件变化
            if stale_paths:
                status = CHECKPOINT_PARTIAL_STALE_STATUS
            # 运行环境变化
            elif mismatch_fields:
                status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
            else:
                status = CHECKPOINT_FULL_VALID_STATUS

    # 创建恢复状态
    resume_state = {
        "status": status,
        "stale_paths": stale_paths,
        "runtime_identity_mismatch_fields": mismatch_fields,
        "stale_summary_invalidations": max(
            len(invalidated),
            int(previous_resume_state.get("stale_summary_invalidations", 0))
            if status == CHECKPOINT_PARTIAL_STALE_STATUS
            else 0,
        ),
    }

    # 写回session
    agent.session["resume_state"] = resume_state
    agent.session["runtime_identity"] = current_runtime_identity(agent)
    return resume_state

# 将checkpoint字典转换成模型能读懂的文本
def render_checkpoint_text(agent):
    checkpoint = current_checkpoint(agent)
    if not checkpoint:
        return ""
    lines = [
        "Task checkpoint:",
        f"- Resume status: {agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
        f"- Current goal: {checkpoint.get('current_goal', '-') or '-'}",
        f"- Current blocker: {checkpoint.get('current_blocker', '-') or '-'}",
        f"- Next step: {checkpoint.get('next_step', '-') or '-'}",
    ]
    key_files = [str(item.get("path", "")).strip() for item in checkpoint.get("key_files", []) if str(item.get("path", "")).strip()]
    lines.append(f"- Key files: {', '.join(key_files) or '-'}")
    if checkpoint.get("completed"):
        lines.append("- Completed: " + " | ".join(str(item) for item in checkpoint.get("completed", [])))
    if checkpoint.get("verified_claims"):
        lines.append("- Verified claims: " + " | ".join(str(item) for item in checkpoint.get("verified_claims", [])))
    if checkpoint.get("open_questions"):
        lines.append("- Open questions: " + " | ".join(str(item) for item in checkpoint.get("open_questions", [])))
    if checkpoint.get("evidence"):
        lines.append("- Evidence: " + " | ".join(str(item) for item in checkpoint.get("evidence", [])))
    if checkpoint.get("excluded"):
        lines.append("- Excluded: " + " | ".join(str(item) for item in checkpoint.get("excluded", [])))
    if agent.resume_state.get("stale_paths"):
        lines.append("- Stale paths: " + ", ".join(agent.resume_state["stale_paths"]))
    summary = str(checkpoint.get("summary", "")).strip()
    if summary:
        lines.append(f"- Summary: {summary}")
    return "\n".join(lines)

# 推断下一步
def infer_next_step(run_state):
    if run_state.status == "completed":
        return "No next step recorded."
    if run_state.stop_reason == "step_limit_reached":
        return "Resume from the latest checkpoint and continue the task."
    if run_state.last_tool:
        return f"Decide the next action after {run_state.last_tool}."
    return "Continue the task from the latest checkpoint."

# 创建 Checkpoint
def create_checkpoint(agent, run_state, user_message, trigger):
    # 获取 checkpoints 状态
    state = checkpoint_state(agent)
    # 获取当前 Checkpoint
    current = current_checkpoint(agent)
    checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]

    key_files = []
    freshness = {}
    # 从 Memory 获取最近使用的文件
    memory_state = agent.memory.to_dict()
    recent_files = memory_state.get(
        "recent_files",
        memory_state.get("working", {}).get("recent_files", []),
    )
    for path in dict.fromkeys(str(path) for path in recent_files if str(path).strip()):
        digest = file_freshness(path, agent.root)
        freshness[path] = digest
        key_files.append({"path": path, "freshness": digest})

    task_progress = agent.task_state.to_dict() if hasattr(agent, "task_state") else {}
    completed = agent.task_state.completed_summaries() if hasattr(agent, "task_state") else []
    if run_state.status == "completed" and run_state.final_answer:
        completed.append(clip(run_state.final_answer, 500))
    current_blocker = agent.task_state.blocker_summary() if hasattr(agent, "task_state") else ""
    if not current_blocker and str(run_state.stop_reason or "") not in ("", "final_answer_returned"):
        current_blocker = str(run_state.stop_reason)
    next_step = str(task_progress.get("next_action", "")).strip() or infer_next_step(run_state)
    verified_claims = [
        clip(str(item.get("claim", "")), 300)
        for item in task_progress.get("claims", [])
        if item.get("verified") and str(item.get("claim", "")).strip()
    ][-8:]
    recent_evidence = [
        clip(str(item.get("summary", "")), 300)
        for item in task_progress.get("evidence", [])
        if str(item.get("summary", "")).strip()
    ][-8:]

    checkpoint = {
        "checkpoint_id": checkpoint_id,
        # 指向上一个检查点，用于形成历史链
        "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
        # 表示 Checkpoint 数据格式版本
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "created_at": now(),

        "current_goal": clip(str(task_progress.get("goal") or user_message), 500),
        "task_state_id": str(task_progress.get("task_id", "")),
        "task_status": str(task_progress.get("status", "")),
        "completed": completed,
        "excluded": [],
        "current_blocker": current_blocker,
        "next_step": next_step,
        "verified_claims": verified_claims,
        "open_questions": list(task_progress.get("open_questions", []))[-8:],
        "evidence": recent_evidence,

        "key_files": key_files,
        "freshness": freshness,
        "summary": f"{trigger}: {clip(str(user_message), 120)}",
        # 记录创建时的模型、工作区、工具等运行环境
        "runtime_identity": current_runtime_identity(agent),
    }
    # 保存这次 Run 对应的 checkpoint_id
    state["items"][checkpoint_id] = checkpoint
    state["current_id"] = checkpoint_id
    run_state.checkpoint_id = checkpoint_id
    if hasattr(run_state, "touch"):
        run_state.touch()
    agent.session["runtime_identity"] = checkpoint["runtime_identity"]

    # Checkpoints live inside the Session, so cap retained history to avoid
    # unbounded growth during long tool-heavy conversations.
    checkpoint_ids = list(state["items"])
    for stale_id in checkpoint_ids[:-MAX_CHECKPOINTS]:
        state["items"].pop(stale_id, None)
    retained_ids = list(state["items"])
    if retained_ids:
        oldest = state["items"][retained_ids[0]]
        if oldest.get("parent_checkpoint_id") not in state["items"]:
            oldest["parent_checkpoint_id"] = ""

    # 创建 Checkpoint 后，会再次保存整个 Session
    agent.session_path = agent.session_store.save(agent.session)
    return checkpoint
