"""CodingAgent：持有一次会话所需的模型、工具、上下文和持久状态。

这个模块负责装配能力并提供 ask() 入口；一次请求何时继续或结束，
由 agent_loop.AgentLoop 控制。模型客户端也不直接执行工具。
"""

import json
import hashlib
import os
import platform
import re
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import checkpoint as checkpointlib
from .memory import LongTermMemory, LongTermMemoryLearner, SessionArchive, WorkingSet, default_working_set_state
from .skills import SkillLearner, SkillStore
from .services.background_jobs import BackgroundJobs
from .services.backlog import Backlog
from . import security as securitylib
from .context_manager import ContextManager
from .source_evidence import SourceEvidence, default_source_evidence_state, summarize_source_text
from .checkpoint import CHECKPOINT_NONE_STATUS
from .prompt_prefix import build_prompt_prefix, tool_signature
from .run_store import RunStore
from .session_store import SessionStore
from .tool_context import ToolContext
from .tool_executor import ToolExecutor
from .events import NullEventSink, UsageForwardingSink
from .progress import ProgressTracker, default_progress
from . import tools as toolkit
from .workspace import IGNORED_PATH_NAMES, WorkspaceContext, now

# 规定 Shell 工具能继承哪些环境变量
DEFAULT_SHELL_ENV_ALLOWLIST = (
    "COMSPEC", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PATHEXT",
    "PWD", "SHELL", "SYSTEMROOT", "TERM", "TMPDIR", "TMP", "TEMP", "USER",
    "USERPROFILE", "WINDIR",
    # 代理：公司网络里 pip/curl 要靠它出网；评测里 shell 命令也得经过出口代理，被拒绝的上网尝试才会被计数
    # （之前 shell 不带代理，请求在内网直接 DNS 失败，egress_denied 一直是 0）
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
)

TOOLSETS = {"core", "full"}

# 一轮最多接受几个原生工具调用：2~4 个只读调用并发执行，其余按顺序执行。
MAX_BATCH_CALLS = 8

# 可独立关闭的功能开关
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    # 接近预算时压缩上下文；context_summary 关掉时只做轻压缩 + 兜底裁剪（结果确定，评测用）。
    "context_reduction": True,
    "context_summary": True,
    "skills": True,
    "skill_learning": True,
}

__all__ = ["CodingAgent", "SessionStore"]


# 一个 CodingAgent 可跨多次 ask() 复用同一 Session。
class CodingAgent:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=50,
        max_new_tokens=8192,
        depth=0,
        max_depth=1,
        read_only=False,
        write_scope=None,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        allowed_tools=None,
        toolset="full",
        event_sink=None,
        machine_mode=False,
        episode_timeout_ms=1_200_000,
        review_changes=False,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store

        # 保存运行策略：这些控制 Agent 的行为边界
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        # 子 agent 记下派它的父 run，trace 里能直接配对（不用再按时间猜）
        self.parent_run_id = ""
        self.read_only = read_only
        # 评测反复观测到「范围蔓延」：只让改 validators.py，模型顺手把
        # tests/test_smoke.py 也改了。提示词管不住它，所以升级成运行时约束。
        self.write_scope = self._normalize_write_scope(write_scope)

        # 补全安全和功能配置
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.allowed_tools = self._normalize_allowed_tools(allowed_tools)
        # core：只暴露编码工具；full：再加长任务待办（backlog）、结论登记、长期记忆、技能、会话检索工具。
        # Python API 默认 full 保持兼容；CLI 默认 core（见 cli.py）。
        if toolset not in TOOLSETS:
            raise ValueError(f"toolset must be one of {sorted(TOOLSETS)}")
        self.toolset = toolset
        self.event_sink = event_sink or NullEventSink()
        self.machine_mode = bool(machine_mode)
        # 交付前审查（review.py）：off / auto（改了非测试源代码才审）/ always。也接受 True/False（= always/off）。
        # Python API 默认 off，CLI 对真实模型默认 auto。
        self.review_mode = self._normalize_review_mode(review_changes)
        # 本次运行的截止时间（AgentLoop 开始时设置）；子 agent 只能用剩下的时间。
        self.run_deadline = None
        self.episode_timeout_ms = int(episode_timeout_ms)
        if self.episode_timeout_ms < 1:
            raise ValueError("episode_timeout_ms must be positive")

        # 自动创建运行期管理模块
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".jaxen" / "runs")
        self.backlog = Backlog(self.root)
        self.background_jobs = BackgroundJobs()

        # 创建或恢复 session（session数据结构）
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "working_set": default_working_set_state(),
            "source_evidence": default_source_evidence_state(),
        }
        self._ensure_session_shape()

        # 持久事实和完整历史不再混进 Session working memory。
        # long_term.json 可精确纠正；archive.sqlite3 只负责跨 Session 检索。
        memory_root = self.root / ".jaxen" / "memory"
        long_term_missing = not (memory_root / "long_term.json").exists()
        self.long_term_memory = LongTermMemory(memory_root, scope_key=str(self.root.resolve()))
        if long_term_missing:
            self.long_term_memory.import_legacy_topics(memory_root / "topics")
        # 冻结快照是刻意的：本轮写入立即落盘，但不会悄悄改变同一 Session 的系统上下文。
        self.long_term_memory_snapshot = self.long_term_memory.render_snapshot()
        self.long_term_memory_learner = LongTermMemoryLearner(self.long_term_memory)
        self.session_archive = SessionArchive(Path(self.session_store.root) / "archive.sqlite3")
        self.session_archive.sync_session(self.session)
        self.session["archive_next_sequence"] = max(
            int(self.session.get("archive_next_sequence", 0)),
            len(self.session.get("history", [])),
        )

        # 从 session 创建 Memory 对象
        self.working_set = WorkingSet(
            self.session.setdefault("working_set", default_working_set_state()),
            workspace_root=self.root,
        )
        self.session["working_set"] = self.working_set.to_dict()
        self.skill_store = SkillStore(self.root / ".jaxen" / "skills")
        # Like durable memory, the index is frozen for prompt stability. A newly
        # learned skill is persisted immediately and appears next session.
        self.skill_index_snapshot = self.skill_store.render_index()
        self.skill_learner = SkillLearner(self.skill_store)
        self.progress = ProgressTracker(self.session.setdefault("progress", default_progress()))
        self.session["progress"] = self.progress.to_dict()
        self.source_evidence = SourceEvidence(
            self.session.setdefault("source_evidence", default_source_evidence_state()),
            workspace_root=self.root,
        )
        self.session["source_evidence"] = self.source_evidence.to_dict()

        # 创建工具系统
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self.tool_executor = ToolExecutor(self)

        # 构建前缀提示词
        self.prefix_state = self.build_prefix()
        # 获得了整个prefix_prompt对象，但是只需要其中的text属性值
        self.prefix = self.prefix_state.text

        # 创建ContextManager，因为此时Jaxen已经拥有prefix、memory、session history、checkpoint等
        # 所以contextmanager可以安全读取并在每轮请求时组装完整prompt
        self.context_manager = ContextManager(self)

        # 检查恢复状态
        self.resume_state = self.evaluate_resume_state()
        # Agent 创建成功后，即使用户还没提问，Session 也会被第一次保存。
        self.session_path = self.session_store.save(self.session)

        self.current_run_state = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.output_recovery_draft = ""
        self.output_recovery_active = False
        self.model_output_correction = ""
        self.last_archive_error = ""
        self.last_auto_skill = {"status": "idle"}
        self.last_auto_memory = {"status": "idle"}
        self._last_tool_result_metadata = {}

    @classmethod
    # 从旧会话恢复 Agent。
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    # 创建或恢复 Session 后调用
    # 会给旧版 session 补齐数据
    def _ensure_session_shape(self):
        # setdefault：如果 key 存在就返回它的值，如果不存在就插入 key 并设为 default，然后返回 default
        if not isinstance(self.session, dict):
            raise ValueError("session must be a dictionary")
        self.session.setdefault("id", datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        self.session.setdefault("created_at", now())
        self.session.setdefault("workspace_root", str(self.root))
        if not isinstance(self.session.get("history"), list):
            self.session["history"] = []
        if not isinstance(self.session.get("working_set"), dict):
            self.session["working_set"] = default_working_set_state()
        if not isinstance(self.session.get("progress"), dict):
            self.session["progress"] = default_progress()
        legacy_source_evidence = self.session.pop("evidence", None)
        if not isinstance(self.session.get("source_evidence"), dict):
            self.session["source_evidence"] = (
                legacy_source_evidence
                if isinstance(legacy_source_evidence, dict)
                else default_source_evidence_state()
            )

        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        items = checkpoints.setdefault("items", {})
        if not isinstance(items, dict):
            items = {}
            checkpoints["items"] = items
        current_id = str(checkpoints.get("current_id", "") or "")
        checkpoints["current_id"] = current_id if current_id in items else ""

        # 当时使用的工作区、运行配置等身份信息
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}

        # 恢复后是否还能安全接着执行
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}

        pending_output = self.session.setdefault("pending_output", {})
        if not isinstance(pending_output, dict):
            self.session["pending_output"] = {}
        pending_input = self.session.setdefault("pending_input", {})
        if not isinstance(pending_input, dict):
            self.session["pending_input"] = {}
        pending_action = self.session.setdefault("pending_action", {})
        if not isinstance(pending_action, dict):
            self.session["pending_action"] = {}

    def current_runtime_identity(self):
        return checkpointlib.current_runtime_identity(self)

    def safe_runtime_info(self):
        client = self.model_client
        return {
            "provider": str(getattr(client, "provider", client.__class__.__name__)),
            "model": str(getattr(client, "model", "unknown")),
            "platform": f"{platform.system()} {platform.release()}",
            "shell": "cmd.exe" if os.name == "nt" else str(os.environ.get("SHELL") or "/bin/sh"),
            "cwd": str(self.workspace.cwd),
            "repo_root": str(self.root),
            "approval_policy": str(self.approval_policy),
            "read_only": bool(self.read_only),
        }

    def checkpoint_state(self):
        return checkpointlib.checkpoint_state(self)

    def current_checkpoint(self):
        return checkpointlib.current_checkpoint(self)

    def invalidate_stale_memory(self):
        # WorkingSet only keeps paths; content claims live in SourceEvidence.
        # Remove stale claims before checkpoint resume evaluates the same files.
        stale_paths = self.source_evidence.invalidate_stale_files()
        self.session["source_evidence"] = self.source_evidence.to_dict()
        return stale_paths

    def evaluate_resume_state(self):
        return checkpointlib.evaluate_resume_state(self)

    def render_checkpoint_text(self):
        return checkpointlib.render_checkpoint_text(self)

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    # 把 Runtime 能提供的依赖包装为 ToolContext，交给 tools.py 构建注册表
    def build_tools(self):
        return toolkit.build_tool_registry(self.tool_context())

    @staticmethod
    def _normalize_allowed_tools(allowed_tools):
        if allowed_tools is None:
            return None
        normalized = tuple(str(name).strip() for name in allowed_tools)
        if not normalized or any(not name for name in normalized):
            raise ValueError("allowed_tools must be a non-empty sequence of tool names")
        return normalized

    def _apply_tool_allowlist(self, tools):
        if self.allowed_tools is None:
            return tools
        legal_names = toolkit.legal_tool_names()
        unknown = [name for name in self.allowed_tools if name not in legal_names]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        allowed = set(self.allowed_tools)
        return {
            name: tool
            for name, tool in tools.items()
            if name in allowed
        }

    def tool_signature(self):
        return tool_signature(self.tools)

    @staticmethod
    def canonical_tool_name(name):
        return toolkit.canonical_tool_name(name)

    @staticmethod
    def tool_requires_approval(name, args):
        return toolkit.tool_requires_approval(name, args)

    # 构建前缀提示词
    def build_prefix(self):
        # system 用的是构建这一刻的工作区快照；之后 self.workspace 每轮刷新，但快照不变。
        self.prefix_workspace = self.workspace
        return build_prompt_prefix(
            workspace=self.prefix_workspace,
            tools=self.tools,
            runtime_info=self.safe_runtime_info(),
        )

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_workspace(self):
        """每轮刷新一次实时工作区（供断点恢复判断新鲜度）；system prompt 不跟着变。"""
        self.workspace = WorkspaceContext.build(self.root)
        return self.workspace

    def refresh_prefix(self):
        """重建 system 的稳定部分。只在工具集或模型变化时调用，平时整个 Session 不变。"""
        self._apply_prefix_state(self.build_prefix())

    def working_memory_text(self):
        return self.working_set.render()

    def long_term_memory_text(self):
        return self.long_term_memory_snapshot

    def skills_text(self):
        return self.skill_index_snapshot


    def memory_text(self):
        """Human-readable memory only; task/evidence deliberately stay out."""
        return self.working_memory_text() + "\n\n" + self.long_term_memory_text()

    # 可以关闭memory、相关记忆检索、上下文压缩
    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    # 将消息写入session的history类似于message
    def record(self, item):
        sequence_no = int(self.session.get("archive_next_sequence", 0))
        item["archive_sequence"] = sequence_no
        self.session["history"].append(item)
        self.session["archive_next_sequence"] = sequence_no + 1
        try:
            item["archive_message_id"] = self.session_archive.append(self.session, sequence_no, item)
            self.last_archive_error = ""
        except Exception as exc:
            # 可检索索引损坏不应吞掉权威的 Session JSON；下一次启动会重新 sync。
            self.last_archive_error = str(exc)
        self.session_path = self.session_store.save(self.session)

    @staticmethod
    def looks_sensitive_env_name(name):
        return securitylib.looks_sensitive_env_name(name)

    def is_secret_env_name(self, name):
        return securitylib.is_secret_env_name(name, secret_env_names=self.secret_env_names)

    def configured_secret_env_items(self):
        return securitylib.configured_secret_env_items(secret_env_names=self.secret_env_names)

    def detected_secret_env_items(self):
        return securitylib.detected_secret_env_items(secret_env_names=self.secret_env_names)

    def secret_env_summary(self):
        return securitylib.secret_env_summary(secret_env_names=self.secret_env_names)

    def detected_secret_env_summary(self):
        return securitylib.detected_secret_env_summary(secret_env_names=self.secret_env_names)

    def redact_text(self, text):
        return securitylib.redact_text(text, secret_env_names=self.secret_env_names)

    def redact_artifact(self, value, key=None):
        return securitylib.redact_artifact(value, key=key, secret_env_names=self.secret_env_names)

    def shell_env(self):
        return securitylib.shell_env(allowlist=self.shell_env_allowlist, root=self.root)

    def build_model_request(self, user_message, tools=None):
        """组装本轮模型请求，并附上 trace/report 需要的元数据。"""
        self.refresh_workspace()
        self.resume_state = self.evaluate_resume_state()
        request, metadata = self.context_manager.prepare(user_message, tools=tools)
        metadata.update(
            {
                "prefix_hash": self.prefix_state.hash,
                "tool_signature": self.prefix_state.tool_signature,
                "tool_count": len(self.tools),
                "instruction_docs": sorted(self.prefix_workspace.project_docs),
                "workspace_fingerprint": self.workspace.fingerprint(),
                "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
                "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return request, metadata

    # event作为参数传入
    def emit_trace(self, run_state, event, payload=None):
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答“这一轮 agent 到底做了什么”。
        self.run_store.append_trace(run_state, payload)
        return payload

    def emit_ui(self, event_type, **payload):
        """Emit a human-facing event without coupling runtime to terminal rendering."""

        try:
            self.event_sink.emit(str(event_type), **payload)
        except Exception as exc:
            # Observability is best-effort: a terminal encoding or renderer bug must
            # never turn a successful model/tool action into an Agent failure.
            self.last_ui_error = str(exc)

    def capture_workspace_snapshot(self):
        snapshot = {}
        for path in self.root.rglob("*"):
            try:
                relative_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if not path.is_file():
                continue
            try:
                snapshot[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:
                continue
        return snapshot

    @staticmethod
    def diff_workspace_snapshots(before, after):
        changed_paths = []
        summaries = []
        all_paths = sorted(set(before) | set(after))
        for path in all_paths:
            if before.get(path) == after.get(path):
                continue
            changed_paths.append(path)
            if path not in before:
                summaries.append(f"created:{path}")
            elif path not in after:
                summaries.append(f"deleted:{path}")
            else:
                summaries.append(f"modified:{path}")
        return changed_paths, summaries

    def create_checkpoint(self, run_state, user_message, trigger):
        return checkpointlib.create_checkpoint(self, run_state, user_message, trigger)

    def infer_next_step(self, run_state):
        return checkpointlib.infer_next_step(run_state)

    # 工具执行后的 Memory 更新
    def update_memory_after_tool(self, name, args, result):
        """Update only current-task navigation; evidence owns file summaries."""
        if not self.feature_enabled("memory"):
            return
        path = args.get("path")
        paths = list(args.get("paths") or []) if name == "read_files" else []
        if not path and not paths:
            return

        if paths:
            for item in paths:
                self._remember_source_evidence(name, item, args)
            self.session["working_set"] = self.working_set.to_dict()
            self.session["source_evidence"] = self.source_evidence.to_dict()
            return

        canonical_path = self.working_set.canonical_path(path)
        # 不是所有工具结果都进入工作记忆。
        # 读文件会生成摘要；写文件/patch 会让旧摘要失效，因为它们可能过期了。
        if name in {"read_file", "write_file", "patch_file"}:
            self.working_set.remember_file(canonical_path)
        if name == "read_file":
            self._remember_source_evidence(name, canonical_path, args)
        self.session["working_set"] = self.working_set.to_dict()
        self.session["source_evidence"] = self.source_evidence.to_dict()

    def _remember_source_evidence(self, name, raw_path, args):
        """Extract a compact source structure from exactly the requested range."""

        canonical = self.working_set.canonical_path(raw_path)
        self.working_set.remember_file(canonical)
        try:
            path = self.path(canonical)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            return {}
        start = int(args.get("start", 1) or 1)
        default_end = 300 if name == "read_files" else 200
        requested_end = int(args.get("end", default_end) or default_end)
        actual_end = min(requested_end, len(lines))
        excerpt = "\n".join(lines[max(0, start - 1):actual_end])
        summary = summarize_source_text(canonical, excerpt)
        return self.source_evidence.record_file(
            canonical,
            summary,
            start=start,
            end=actual_end,
            total_lines=len(lines),
            source_tool=name,
        )


    def auto_capture_long_term_memory(self, user_message):
        if not self.feature_enabled("memory"):
            self.last_auto_memory = {"status": "skipped", "reason": "feature_disabled", "results": []}
            return self.last_auto_memory
        self.last_auto_memory = self.long_term_memory_learner.capture(
            user_message,
            session_id=self.session["id"],
        )
        return self.last_auto_memory

    # Agent 对外的主入口；单次请求的生命周期交给 AgentLoop。
    def ask(self, user_message, responses=None, request_id=None):
        from .agent_loop import AgentLoop
        # `/review [说明]`：CLI、JSONL（界面）都走这里；不进主循环，不改代码
        message = str(user_message or "").strip()
        if message == "/review" or message.startswith("/review "):
            return self.review_workspace(message[len("/review"):].strip())
        # 调用agent loop中的run
        return AgentLoop(self).run(
            user_message,
            responses=responses,
            request_id=request_id,
        )

    # 工具执行入口：由 loop 调用，真正的校验与执行交给 ToolExecutor。
    def execute_tool(self, name, args):
        name = self.canonical_tool_name(name)
        result = self.tool_executor.execute(name, args)
        # Tool output may contain a configured secret (for example when a shell
        # command prints environment state). Redact before Session, prompt, UI,
        # task evidence or trace can observe it.
        redacted_content = self.redact_text(result.content)
        if redacted_content != result.content:
            from .tool_executor import ToolExecutionResult
            result = ToolExecutionResult(content=redacted_content, metadata=dict(result.metadata))
        self._last_tool_result_metadata = dict(result.metadata)
        self.progress.observe_tool(name, args, result.content, result.metadata)
        self.session["progress"] = self.progress.to_dict()
        self.session_path = self.session_store.save(self.session)
        return result

    def delivery_blockers(self):
        blockers = []
        tool_blocker = self.tool_executor.completion_blocker()
        # 与 progress 的口径一致：只有可重试的失败才算"还没解决"。被策略
        # 拒绝的调用（如 path_escape）本就不该重试，模型绕开它就是正确恢复。
        if tool_blocker and tool_blocker.get("retryable"):
            blockers.append(
                f"unresolved tool failure {tool_blocker.get('name')}:{tool_blocker.get('error_code')}"
            )
        blockers.extend(
            self.progress.delivery_blockers(
                background_pending=self.background_jobs.pending_count(),
            )
        )
        return list(dict.fromkeys(blockers))

    def run_tool(self, name, args):
        """执行一次工具调用，并在执行前后套上完整护栏。

        为什么存在：
        在 agent 系统里，真正危险的不是“模型会不会想调用工具”，而是
        “平台有没有在执行前把边界守住”。这个函数就是工具层的总闸口：
        所有工具调用都必须先经过它，不能让模型直接碰到底层函数。

        输入 / 输出：
        - 输入：工具名 `name`，参数字典 `args`
        - 输出：字符串结果。无论是成功结果还是错误信息，都会统一返回文本，
          这样模型下一轮都能继续消费这份反馈。

        在 agent 链路里的位置：
        它位于 `ask()` 的“模型决定要调用工具”之后，是控制循环里真正把模型
        意图落到外部世界的一步。因此这里串起了几乎所有安全与可控设计：
        工具是否存在、参数是否合法、是否重复、是否需要审批、执行结果是否裁剪、
        是否需要回写记忆。
        """
        return self.execute_tool(name, args).content

    def repeated_tool_call(self, name, args):
        # agent 很常见的一种坏循环，是在没有新信息的情况下反复发起同一调用。
        # 这里提前挡掉最简单的这种循环。
        name = self.canonical_tool_name(name)
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        recent = tool_events[-2:]
        return all(item["name"] == name and item["args"] == args for item in recent)

    @staticmethod
    def new_request_id():
        return "req_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    # 创建报告
    def build_report(self, run_state):
        # report 是一次运行的最终摘要；
        # 和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        return {
            "run_id": run_state.run_id,
            "request_id": run_state.request_id,
            "status": run_state.status,
            "stop_reason": run_state.stop_reason,
            "final_answer": run_state.final_answer,
            "tool_steps": run_state.tool_steps,
            "attempts": run_state.attempts,
            "checkpoint_id": run_state.checkpoint_id,
            "resume_status": run_state.resume_status,
            "run_state": run_state.to_dict(),
            "prompt_metadata": self.last_prompt_metadata,
            "auto_skill": dict(self.last_auto_skill),
            "auto_memory": dict(self.last_auto_memory),
            "redacted_env": self.detected_secret_env_summary(),
            "progress": self.progress.to_dict(),
            "source_evidence": self.source_evidence.to_dict(),
        }

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self.tool_context(), self.canonical_tool_name(name), args)

    def tool_context(self):
        return ToolContext(
            root=self.root,
            path_resolver=self.path,
            shell_env_provider=self.shell_env,
            runtime_info_provider=self.safe_runtime_info,
            depth=self.depth,
            max_depth=self.max_depth,
            spawn_investigation=self.spawn_investigation,
            backlog=self.backlog,
            progress=self.progress,
            background_jobs=self.background_jobs,
            long_term_memory=self.long_term_memory,
            session_archive=self.session_archive,
            skill_store=self.skill_store,
            session_id=str(self.session["id"]),
            toolset=self.toolset,
        )

    @staticmethod
    def _normalize_review_mode(value):
        from .review import REVIEW_MODES

        if isinstance(value, bool) or value is None:
            return "always" if value else "off"
        if value not in REVIEW_MODES:
            raise ValueError(f"review mode must be one of {REVIEW_MODES}")
        return value

    def remaining_time_ms(self):
        """本次运行剩余的毫秒数；还没开始运行时就是完整时长。"""
        if self.run_deadline is None:
            return self.episode_timeout_ms
        return max(1_000, int((self.run_deadline - time.monotonic()) * 1000))

    def readonly_child(self, max_steps):
        """只读子 agent：调查（investigate）和审查（review）共用。只能读和搜，不能改文件、不能执行命令。

        子 agent 的事件只把 token 用量转给父 agent：预算统计要算上它花的钱，
        但它的工具卡片和回答不该混进父 agent 的界面。
        """
        child = CodingAgent(
            model_client=self.model_client,
            workspace=self.workspace,
            session_store=self.session_store,
            run_store=self.run_store,
            approval_policy="never",
            max_steps=int(max_steps),
            max_new_tokens=self.max_new_tokens,
            # 同步执行，父 agent 在等：子 agent 只能用父 agent 剩下的时间，不能把总时长撑破
            episode_timeout_ms=self.remaining_time_ms(),
            depth=self.depth + 1,
            max_depth=self.max_depth,
            read_only=True,
            allowed_tools=("list_files", "read_file", "read_files", "search", "glob", "repo_map", "runtime_info"),
            secret_env_names=self.secret_env_names,
            shell_env_allowlist=self.shell_env_allowlist,
            event_sink=UsageForwardingSink(self.event_sink),
        )
        child.parent_run_id = str(getattr(getattr(self, "current_run_state", None), "run_id", "") or "")
        return child

    def review_workspace(self, request):
        """`/review`：审查当前工作区的未提交改动，只出报告、不改代码。"""
        from .review import review_report, run_review, workspace_diff

        diff = workspace_diff(self.root)
        if not diff.strip():
            return "## Code review\n\nNo uncommitted changes to review."
        return review_report(run_review(self, request or "Review the uncommitted changes in this workspace.", diff))

    def spawn_investigation(self, args):
        """调查子 agent：同步运行，结果装进信封返回给父 agent（见 subagent.py）。"""
        from .subagent import envelope, run_child

        task = str(args.get("task", "")).strip()
        _, report = run_child(self, "investigate", task, int(args.get("max_steps", toolkit.DEFAULT_INVESTIGATE_STEPS)))
        return envelope(report)

    def auto_deposit_skill(self, user_message, final_answer, run_state):
        if not self.feature_enabled("skills") or not self.feature_enabled("skill_learning"):
            self.last_auto_skill = {"status": "skipped", "reason": "feature_disabled"}
            return self.last_auto_skill
        self.last_auto_skill = self.skill_learner.consider(
            user_message,
            final_answer,
            run_state,
            self.source_evidence.to_dict(),
        )
        if self.last_auto_skill.get("status") == "created":
            self.emit_trace(run_state, "skill_auto_deposited", self.last_auto_skill)
            self.emit_ui("skill_learned", **self.last_auto_skill)
        return self.last_auto_skill

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        if self.machine_mode:
            fingerprint = self.tool_executor._call_fingerprint(name, args)
            pending = dict(self.session.get("pending_action", {}) or {})
            if pending and pending.get("fingerprint") != fingerprint:
                return False
            if not pending:
                input_id = "input_approval_" + uuid.uuid4().hex[:10]
                pending = {
                    "kind": "approval",
                    "input_id": input_id,
                    "name": self.canonical_tool_name(name),
                    # Session is the execution source of truth. Redaction is
                    # applied only when this value leaves the runtime.
                    "args": json.loads(json.dumps(args, ensure_ascii=False)),
                    "fingerprint": fingerprint,
                    "execution_state": "pending",
                    "created_at": now(),
                }
                self.session["pending_action"] = pending
                self.session["pending_input"] = {
                    "kind": "approval",
                    "input_id": input_id,
                    "question": f"Approve {pending['name']} with the exact recorded arguments?",
                }
                self.session_path = self.session_store.save(self.session)
            self.emit_ui(
                "approval_requested",
                input_id=pending["input_id"],
                name=pending["name"],
                args=self.redact_artifact(pending["args"]),
            )
            return None
        self.emit_ui("approval_requested", name=name, args=args)
        try:
            answer = input("Allow this action? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def request_clarification(self, question):
        input_id = "input_text_" + uuid.uuid4().hex[:10]
        self.session["pending_input"] = {
            "kind": "clarification",
            "input_id": input_id,
            "question": str(question),
            "created_at": now(),
        }
        self.session_path = self.session_store.save(self.session)
        return dict(self.session["pending_input"])

    @staticmethod
    def _response_value(response):
        for key in ("value", "text", "decision"):
            if key in response and str(response.get(key, "")).strip():
                return str(response[key]).strip()
        if "approved" in response:
            return "approve" if bool(response["approved"]) else "deny"
        return ""

    def resolve_pending_input(self, responses=None, fallback_text=""):
        """Resolve one persisted input request without trusting free-form approval."""

        pending_input = dict(self.session.get("pending_input", {}) or {})
        pending_action = dict(self.session.get("pending_action", {}) or {})
        if not pending_input:
            return {"status": "none", "text": str(fallback_text)}

        responses = [item for item in (responses or []) if isinstance(item, dict)]
        input_id = str(pending_input.get("input_id", ""))
        response = next(
            (
                item
                for item in responses
                if str(item.get("inputId") or item.get("input_id") or "") == input_id
            ),
            None,
        )
        if pending_input.get("kind") == "clarification":
            value = self._response_value(response or {}) or str(fallback_text).strip()
            if not value:
                return {"status": "waiting", "input": pending_input}
            self.session["pending_input"] = {}
            self.session_path = self.session_store.save(self.session)
            return {"status": "clarified", "text": value, "input": pending_input}

        if not response:
            return {"status": "waiting", "input": pending_input}
        decision = self._response_value(response).casefold()
        if decision in {"deny", "denied", "reject", "rejected", "no", "false"}:
            self.session["pending_action"] = {}
            self.session["pending_input"] = {}
            self.session_path = self.session_store.save(self.session)
            return {"status": "denied", "action": pending_action}
        if decision not in {"approve", "approved", "allow", "yes", "true"}:
            return {"status": "waiting", "input": pending_input}

        if pending_action.get("execution_state") != "pending":
            return {"status": "invalid", "reason": "pending action is not executable"}
        expected = str(pending_action.get("fingerprint", ""))
        actual = self.tool_executor._call_fingerprint(
            pending_action.get("name", ""), pending_action.get("args", {})
        )
        if not expected or expected != actual:
            return {"status": "invalid", "reason": "pending action fingerprint mismatch"}

        self.session["pending_action"]["execution_state"] = "executing"
        self.session_path = self.session_store.save(self.session)
        return {
            "status": "approved",
            "action": pending_action,
            "expected_fingerprint": expected,
        }

    def execute_pending_action(self, action, expected_fingerprint):
        result = self.tool_executor.execute_approved(
            action["name"], action["args"], expected_fingerprint
        )
        redacted_content = self.redact_text(result.content)
        if redacted_content != result.content:
            from .tool_executor import ToolExecutionResult

            result = ToolExecutionResult(
                content=redacted_content,
                metadata=dict(result.metadata),
            )
        self._last_tool_result_metadata = dict(result.metadata)
        self.progress.observe_tool(
            action["name"], action["args"], result.content, result.metadata
        )
        self.session["progress"] = self.progress.to_dict()
        self.session["pending_action"] = {}
        self.session["pending_input"] = {}
        self.session_path = self.session_store.save(self.session)
        return result

    @staticmethod
    def parse(raw):
        """把模型原始输出解析成 Agent 可执行的动作或最终答案。

        为什么存在：
        模型输出首先是自然语言文本，而控制循环需要的是结构化决策：
        “这是工具调用”还是“这是最终答案”。如果没有这层解析，后面的工具校验、
        审批和执行链路就没法可靠工作。

        输入 / 输出：
        - 输入：模型返回的原始文本 `raw`
        - 输出：`(kind, payload)`，其中 `kind` 可能是 `tool`、`final`、`retry`

        在 agent 链路里的位置：
        它位于 `model_client.complete()` 之后、`run_tool()` 之前，是模型输出
        进入平台控制流的第一道结构化关口。
        """
        raw = str(raw)
        if CodingAgent.looks_like_internal_tool_protocol(raw):
            return "retry", CodingAgent.retry_notice(
                "model returned an internal tool-call protocol instead of a user-facing answer"
            )
        if raw.startswith("<tool_batch>"):
            if not raw.endswith("</tool_batch>"):
                return "retry", CodingAgent.retry_notice("model returned an incomplete tool batch")
            try:
                actions = json.loads(raw[len("<tool_batch>"):-len("</tool_batch>")])
            except (TypeError, ValueError):
                return "retry", CodingAgent.retry_notice("model returned malformed tool batch JSON")
            if not isinstance(actions, list) or not 2 <= len(actions) <= MAX_BATCH_CALLS or any(
                not isinstance(action, dict)
                or not str(action.get("id", "")).strip()
                or not str(action.get("name", "")).strip()
                or not isinstance(action.get("args"), dict)
                for action in actions
            ):
                return "retry", CodingAgent.retry_notice(
                    f"tool batch needs 2 to {MAX_BATCH_CALLS} calls with id, name, and args"
                )
            return "tool_batch", actions
        # 这里支持两种工具格式：
        # 1. <tool>...</tool> 里包 JSON，适合简短调用
        # 2. XML 风格属性/子标签，适合写文件这类多行内容
        if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
            if "</tool>" not in raw:
                return "retry", CodingAgent.retry_notice("model returned an incomplete <tool> call")
            body = CodingAgent.extract(raw, "tool")
            try:
                # 使用标准Json解析器
                payload = json.loads(body)
            except Exception:
                return "retry", CodingAgent.retry_notice("model returned malformed tool JSON")
            if not isinstance(payload, dict):
                return "retry", CodingAgent.retry_notice("tool payload must be a JSON object")
            if not str(payload.get("name", "")).strip():
                return "retry", CodingAgent.retry_notice("tool payload is missing a tool name")
            args = payload.get("args", {})
            if args is None:
                payload["args"] = {}
            elif not isinstance(args, dict):
                return "retry", CodingAgent.retry_notice()
            return CodingAgent.normalize_protocol_action(payload)
        if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
            payload = CodingAgent.parse_xml_tool(raw)
            if payload is not None:
                return CodingAgent.normalize_protocol_action(payload)
            return "retry", CodingAgent.retry_notice()
        if "<final>" in raw:
            if "</final>" not in raw:
                return "retry", CodingAgent.retry_notice("model returned an incomplete <final> answer")
            final = CodingAgent.extract(raw, "final").strip()
            if CodingAgent.looks_like_placeholder_answer(final):
                return "retry", CodingAgent.retry_notice(
                    "model returned a placeholder instead of a meaningful <final> answer"
                )
            if final:
                return "final", final
            return "retry", CodingAgent.retry_notice("model returned an empty <final> answer")
        if "<needs_user_input>" in raw:
            if "</needs_user_input>" not in raw:
                return "retry", CodingAgent.retry_notice("model returned an incomplete <needs_user_input> response")
            question = CodingAgent.extract(raw, "needs_user_input").strip()
            if question:
                return "needs_user_input", question
            return "retry", CodingAgent.retry_notice("model returned an empty <needs_user_input> answer")
        if "<blocked>" in raw:
            if "</blocked>" not in raw:
                return "retry", CodingAgent.retry_notice("model returned an incomplete <blocked> response")
            reason = CodingAgent.extract(raw, "blocked").strip()
            if reason:
                return "blocked", reason
            return "retry", CodingAgent.retry_notice("model returned an empty <blocked> answer")
        raw = raw.strip()
        if CodingAgent.looks_like_placeholder_answer(raw):
            return "retry", CodingAgent.retry_notice(
                "model returned a placeholder instead of a meaningful answer"
            )
        if raw:
            return "final", raw
        return "retry", CodingAgent.retry_notice("model returned an empty response")

    @staticmethod
    def normalize_protocol_action(payload):
        """Lift control-plane pseudo calls out of the executable tool path.

        Some OpenAI-compatible providers may emit ``needs_user_input`` as a
        native function call even though it is intentionally absent from the
        tool registry. Treating that as an unknown tool turns a valid pause
        into a tool failure. It is a protocol state, so normalize it before the
        ToolExecutor can see it.
        """

        name = str(payload.get("name", "")).strip()
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        if name != "needs_user_input":
            return "tool", payload
        question = next(
            (
                str(args.get(key, "")).strip()
                for key in ("question", "text", "prompt")
                if str(args.get(key, "")).strip()
            ),
            "",
        )
        if question:
            return "needs_user_input", question
        return "retry", CodingAgent.retry_notice(
            "needs_user_input requires a non-empty question"
        )

    @staticmethod
    def looks_like_internal_tool_protocol(raw):
        """Reject provider control syntax that must never reach the terminal."""

        text = str(raw).lstrip()
        control_prefix = text[:240]
        has_provider_marker = any(
            marker in control_prefix
            for marker in ("<｜｜DSML｜｜", "<|DSML|", "<｜tool", "<|tool")
        )
        return has_provider_marker and (
            "tool_calls" in control_prefix or "invoke name=" in control_prefix
        )

    @staticmethod
    def looks_like_placeholder_answer(raw):
        """Reject only unambiguous empty-answer placeholders.

        Keep this deliberately narrow: short valid answers such as ``是`` or
        ``ok`` remain legal, while strings made only of dots/ellipsis characters
        cannot mark a task completed.
        """

        compact = re.sub(r"\s+", "", str(raw))
        return bool(compact) and all(character in ".。…·" for character in compact)

    @staticmethod
    def retry_notice(problem=None):
        prefix = "Runtime notice"
        if problem:
            prefix += f": {problem}"
        else:
            prefix += ": model returned malformed tool output"
        return (
            f"{prefix}. Reply with a valid <tool> call, <needs_user_input>, <blocked>, or a non-empty <final> answer. "
            'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
        )

    # 解析xml文本
    @staticmethod
    def parse_xml_tool(raw):
        match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
        if not match:
            return None
        attrs = CodingAgent.parse_attrs(match.group("attrs"))
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None

        body = match.group("body")
        args = dict(attrs)
        for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path", "question"):
            if f"<{key}>" in body:
                args[key] = CodingAgent.extract_raw(body, key)

        body_text = body.strip("\n")
        if name == "write_file" and "content" not in args and body_text:
            args["content"] = body_text
        if name == "investigate" and "task" not in args and body_text:
            args["task"] = body_text.strip()
        return {"name": name, "args": args}

    @staticmethod
    def parse_attrs(text):
        attrs = {}
        for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
            attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return attrs

    # 提取工具调用返回内容
    @staticmethod
    def extract(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:].strip()
        return text[start:end].strip()

    @staticmethod
    def extract_raw(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:]
        return text[start:end]

    def reset(self):
        self.session["history"] = []
        self.session["context"] = {}
        self.session["working_set"].clear()
        self.session["working_set"].update(default_working_set_state())
        self.session["checkpoints"] = {"current_id": "", "items": {}}
        self.session["resume_state"] = {}
        self.session["pending_output"] = {}
        self.session["progress"] = default_progress()
        self.session["source_evidence"] = default_source_evidence_state()
        self.working_set = WorkingSet(self.session["working_set"], workspace_root=self.root)
        self.progress = ProgressTracker(self.session["progress"])
        self.source_evidence = SourceEvidence(self.session["source_evidence"], workspace_root=self.root)
        self.resume_state = self.evaluate_resume_state()
        self.session_path = self.session_store.save(self.session)

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

    @staticmethod
    def _normalize_write_scope(write_scope):
        """None 表示不限制；给了列表就必须非空，避免"写了个空列表以为放开了"。"""

        if write_scope is None:
            return None
        patterns = tuple(str(item).strip().replace("\\", "/") for item in write_scope)
        if not patterns or any(not item for item in patterns):
            raise ValueError("write_scope must be a non-empty sequence of path patterns")
        return patterns


    def write_scope_violations(self, paths):
        """返回 paths 中不在允许写入范围内的仓库相对路径。"""

        if self.write_scope is None:
            return []
        violations = []
        for raw_path in paths:
            relative = str(raw_path).replace("\\", "/").lstrip("./")
            if not relative:
                continue
            allowed = any(
                relative == pattern or PurePosixPath(relative).match(pattern)
                for pattern in self.write_scope
            )
            if not allowed and relative not in violations:
                violations.append(relative)
        return violations


