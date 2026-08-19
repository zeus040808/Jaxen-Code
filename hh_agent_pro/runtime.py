"""Agent 运行时核心逻辑。

HH 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import hashlib
import os
import platform
import re
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import checkpoint as checkpointlib
from .memory import CuratedMemoryStore, DurableMemoryLearner, SessionArchive, WorkingMemory, default_working_state
from .skills import SkillLearner, SkillStore
from .services.background_tasks import BackgroundTaskManager
from .services.cron_scheduler import CronScheduler
from .services.project_tasks import ProjectTaskStore
from . import security as securitylib
from .context_manager import ContextManager
from .evidence import EvidenceLedger, default_evidence_state, summarize_source_text
from .checkpoint import CHECKPOINT_NONE_STATUS
from .prompt_prefix import build_prompt_prefix, tool_signature
from .run_store import RunStore
from .session_store import SessionStore
from .tool_context import ToolContext
from .tool_executor import ToolExecutor
from .streaming import NullEventSink
from .task_state import TaskState, default_task_state
from . import tools as toolkit
from .workspace import IGNORED_PATH_NAMES, MAX_HISTORY, WorkspaceContext, clip, now

# 规定 Shell 工具能继承哪些环境变量
DEFAULT_SHELL_ENV_ALLOWLIST = (
    "COMSPEC", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PATHEXT",
    "PWD", "SHELL", "SYSTEMROOT", "TERM", "TMPDIR", "TMP", "TEMP", "USER",
    "USERPROFILE", "WINDIR",
)

# 可独立关闭的功能开关
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "context_reduction": True,
    "prompt_cache": True,
    "skills": True,
    "skill_learning": True,
}

__all__ = ["HH", "SessionStore"]


# 装配完整agent
class HH:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=12,
        max_new_tokens=2048,
        depth=0,
        max_depth=1,
        read_only=False,
        write_scope=None,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        allowed_tools=None,
        event_sink=None,
        machine_mode=False,
        episode_timeout_ms=180000,
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
        self.event_sink = event_sink or NullEventSink()
        self.machine_mode = bool(machine_mode)
        self.episode_timeout_ms = int(episode_timeout_ms)
        if self.episode_timeout_ms < 1:
            raise ValueError("episode_timeout_ms must be positive")

        # 自动创建运行期管理模块
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".hh" / "runs")
        self.project_tasks = ProjectTaskStore(self.root)
        self.background_tasks = BackgroundTaskManager()
        self.cron_scheduler = CronScheduler(self.root)

        # 创建或恢复 session（session数据结构）
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": default_working_state(),
            "source_evidence": default_evidence_state(),
        }
        self._ensure_session_shape()

        # 持久事实和完整历史不再混进 Session working memory。
        # curated.json 可精确纠正；archive.sqlite3 只负责跨 Session 检索。
        memory_root = self.root / ".hh" / "memory"
        curated_missing = not (memory_root / "curated.json").exists()
        self.curated_memory = CuratedMemoryStore(memory_root, scope_key=str(self.root.resolve()))
        if curated_missing:
            self.curated_memory.import_legacy_topics(memory_root / "topics")
        # 冻结快照是刻意的：本轮写入立即落盘，但不会悄悄改变同一 Session 的系统上下文。
        self.curated_memory_snapshot = self.curated_memory.render_snapshot()
        self.durable_memory_learner = DurableMemoryLearner(self.curated_memory)
        self.session_archive = SessionArchive(Path(self.session_store.root) / "archive.sqlite3")
        self.session_archive.sync_session(self.session)
        self.session["archive_next_sequence"] = max(
            int(self.session.get("archive_next_sequence", 0)),
            len(self.session.get("history", [])),
        )

        # 从 session 创建 Memory 对象
        self.memory = WorkingMemory(
            self.session.setdefault("memory", default_working_state()),
            workspace_root=self.root,
        )
        self.session["memory"] = self.memory.to_dict()
        self.skill_store = SkillStore(self.root / ".hh" / "skills")
        # Like durable memory, the index is frozen for prompt stability. A newly
        # learned skill is persisted immediately and appears next session.
        self.skill_index_snapshot = self.skill_store.render_index()
        self.skill_learner = SkillLearner(self.skill_store)
        self.task_state = TaskState(
            self.session.setdefault("task_state", default_task_state()),
            self.project_tasks,
        )
        self.session["task_state"] = self.task_state.to_dict()
        self.source_evidence = EvidenceLedger(
            self.session.setdefault("source_evidence", default_evidence_state()),
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

        # 创建ContextManager，因为此时HH已经拥有prefix、memory、session history、checkpoint等
        # 所以contextmanager可以安全读取并在每轮请求时组装完整prompt
        self.context_manager = ContextManager(self)

        # 检查恢复状态
        self.resume_state = self.evaluate_resume_state()
        # 只要 HH 成功创建，即使用户还没提问，Session 也会被第一次保存
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
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }

    @classmethod
    # 从旧会话创建 HH
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
        if not isinstance(self.session.get("memory"), dict):
            self.session["memory"] = default_working_state()
        if not isinstance(self.session.get("task_state"), dict):
            self.session["task_state"] = default_task_state()
        legacy_source_evidence = self.session.pop("evidence", None)
        if not isinstance(self.session.get("source_evidence"), dict):
            self.session["source_evidence"] = (
                legacy_source_evidence
                if isinstance(legacy_source_evidence, dict)
                else default_evidence_state()
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
        # WorkingMemory only keeps paths; content claims live in EvidenceLedger.
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
        return build_prompt_prefix(
            workspace=self.workspace,
            tools=self.tools,
            runtime_info=self.safe_runtime_info(),
            native_tools=bool(getattr(self.model_client, "supports_native_tools", False)),
        )

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    # 重新执行build，拿到仓库最新状态
    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        # 旧指纹
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(self.root)
        # 新指纹
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)

        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def working_memory_text(self):
        return self.memory.render()

    def durable_memory_text(self):
        return self.curated_memory_snapshot

    def skills_text(self):
        return self.skill_index_snapshot

    def task_state_text(self):
        return self.task_state.render()

    def evidence_text(self):
        return self.source_evidence.render()

    def memory_text(self):
        """Human-readable memory only; task/evidence deliberately stay out."""
        return self.working_memory_text() + "\n\n" + self.durable_memory_text()

    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    # 可以关闭memory、相关记忆检索、上下文压缩
    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

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

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        # 刷新前缀提示词
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把“这轮 prompt 是怎么拼出来的”连同缓存相关状态一起记下来，
        # 后面 trace/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。

        # 随后补充 Runtime 层信息
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
                "provider_request_mode": (
                    "structured-native-tools"
                    if bool(getattr(self.model_client, "supports_native_tools", False))
                    else "text-compatibility"
                ),
                "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
                "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return prompt, metadata

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
            self.session["memory"] = self.memory.to_dict()
            self.session["source_evidence"] = self.source_evidence.to_dict()
            return

        canonical_path = self.memory.canonical_path(path)
        # 不是所有工具结果都进入工作记忆。
        # 读文件会生成摘要；写文件/patch 会让旧摘要失效，因为它们可能过期了。
        if name in {"read_file", "write_file", "patch_file"}:
            self.memory.remember_file(canonical_path)
        if name == "read_file":
            self._remember_source_evidence(name, canonical_path, args)
        self.session["memory"] = self.memory.to_dict()
        self.session["source_evidence"] = self.source_evidence.to_dict()

    def _remember_source_evidence(self, name, raw_path, args):
        """Extract a compact source structure from exactly the requested range."""

        canonical = self.memory.canonical_path(raw_path)
        self.memory.remember_file(canonical)
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

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args, result)

    def auto_capture_durable_memory(self, user_message):
        if not self.feature_enabled("memory"):
            self.last_auto_memory = {"status": "skipped", "reason": "feature_disabled", "results": []}
            return self.last_auto_memory
        self.last_auto_memory = self.durable_memory_learner.capture(
            user_message,
            session_id=self.session["id"],
        )
        return self.last_auto_memory

    # Runtime 对外的主入口
    def ask(self, user_message, responses=None, request_id=None):
        from .agent_loop import AgentLoop
        # 调用agent loop中的run
        return AgentLoop(self).run(
            user_message,
            responses=responses,
            request_id=request_id,
        )

    # 工具执行
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
        self.task_state.observe_tool(name, args, result.content, result.metadata)
        self.session["task_state"] = self.task_state.to_dict()
        self.session_path = self.session_store.save(self.session)
        return result

    def delivery_blockers(self):
        blockers = []
        tool_blocker = self.tool_executor.completion_blocker()
        if tool_blocker:
            blockers.append(
                f"unresolved tool failure {tool_blocker.get('name')}:{tool_blocker.get('error_code')}"
            )
        blockers.extend(
            self.task_state.delivery_blockers(
                background_pending=self.background_tasks.pending_count(),
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
            "task_state": self.task_state.to_dict(),
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
            spawn_delegate=self.spawn_delegate,
            task_store=self.project_tasks,
            task_state=self.task_state,
            background_tasks=self.background_tasks,
            cron_scheduler=self.cron_scheduler,
            curated_memory=self.curated_memory,
            session_archive=self.session_archive,
            skill_store=self.skill_store,
            session_id=str(self.session["id"]),
        )

    def spawn_delegate(self, args):
        task = str(args.get("task", "")).strip()
        child = HH(
            model_client=self.model_client,
            workspace=self.workspace,
            session_store=self.session_store,
            run_store=self.run_store,
            approval_policy="never",
            max_steps=int(args.get("max_steps", 3)),
            max_new_tokens=self.max_new_tokens,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            read_only=True,
            secret_env_names=self.secret_env_names,
            shell_env_allowlist=self.shell_env_allowlist,
        )
        # 委派的目标是“调查”，不是“放权执行”。
        # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
        child.memory.set_task_summary(task)
        child.session["memory"] = child.memory.to_dict()
        return "delegate_result:\n" + child.ask(task)

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
        self.task_state.observe_tool(
            action["name"], action["args"], result.content, result.metadata
        )
        self.session["task_state"] = self.task_state.to_dict()
        self.session["pending_action"] = {}
        self.session["pending_input"] = {}
        self.session_path = self.session_store.save(self.session)
        return result

    @staticmethod
    def parse(raw):
        """把模型原始输出解析成 runtime 可执行的动作或最终答案。

        为什么存在：
        模型输出首先是自然语言文本，而 runtime 需要的是结构化决策：
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
        if HH.looks_like_internal_tool_protocol(raw):
            return "retry", HH.retry_notice(
                "model returned an internal tool-call protocol instead of a user-facing answer"
            )
        # 这里支持两种工具格式：
        # 1. <tool>...</tool> 里包 JSON，适合简短调用
        # 2. XML 风格属性/子标签，适合写文件这类多行内容
        if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
            if "</tool>" not in raw:
                return "retry", HH.retry_notice("model returned an incomplete <tool> call")
            body = HH.extract(raw, "tool")
            try:
                # 使用标准Json解析器
                payload = json.loads(body)
            except Exception:
                return "retry", HH.retry_notice("model returned malformed tool JSON")
            if not isinstance(payload, dict):
                return "retry", HH.retry_notice("tool payload must be a JSON object")
            if not str(payload.get("name", "")).strip():
                return "retry", HH.retry_notice("tool payload is missing a tool name")
            args = payload.get("args", {})
            if args is None:
                payload["args"] = {}
            elif not isinstance(args, dict):
                return "retry", HH.retry_notice()
            return HH.normalize_protocol_action(payload)
        if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
            payload = HH.parse_xml_tool(raw)
            if payload is not None:
                return HH.normalize_protocol_action(payload)
            return "retry", HH.retry_notice()
        if "<final>" in raw:
            if "</final>" not in raw:
                return "retry", HH.retry_notice("model returned an incomplete <final> answer")
            final = HH.extract(raw, "final").strip()
            if HH.looks_like_placeholder_answer(final):
                return "retry", HH.retry_notice(
                    "model returned a placeholder instead of a meaningful <final> answer"
                )
            if final:
                return "final", final
            return "retry", HH.retry_notice("model returned an empty <final> answer")
        if "<needs_user_input>" in raw:
            if "</needs_user_input>" not in raw:
                return "retry", HH.retry_notice("model returned an incomplete <needs_user_input> response")
            question = HH.extract(raw, "needs_user_input").strip()
            if question:
                return "needs_user_input", question
            return "retry", HH.retry_notice("model returned an empty <needs_user_input> answer")
        if "<blocked>" in raw:
            if "</blocked>" not in raw:
                return "retry", HH.retry_notice("model returned an incomplete <blocked> response")
            reason = HH.extract(raw, "blocked").strip()
            if reason:
                return "blocked", reason
            return "retry", HH.retry_notice("model returned an empty <blocked> answer")
        raw = raw.strip()
        if HH.looks_like_placeholder_answer(raw):
            return "retry", HH.retry_notice(
                "model returned a placeholder instead of a meaningful answer"
            )
        if raw:
            return "final", raw
        return "retry", HH.retry_notice("model returned an empty response")

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
        return "retry", HH.retry_notice(
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
        attrs = HH.parse_attrs(match.group("attrs"))
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None

        body = match.group("body")
        args = dict(attrs)
        for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path", "question"):
            if f"<{key}>" in body:
                args[key] = HH.extract_raw(body, key)

        body_text = body.strip("\n")
        if name == "write_file" and "content" not in args and body_text:
            args["content"] = body_text
        if name == "delegate" and "task" not in args and body_text:
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
        self.session["memory"].clear()
        self.session["memory"].update(default_working_state())
        self.session["checkpoints"] = {"current_id": "", "items": {}}
        self.session["resume_state"] = {}
        self.session["pending_output"] = {}
        self.session["task_state"] = default_task_state()
        self.session["source_evidence"] = default_evidence_state()
        self.memory = WorkingMemory(self.session["memory"], workspace_root=self.root)
        self.task_state = TaskState(self.session["task_state"], self.project_tasks)
        self.source_evidence = EvidenceLedger(self.session["source_evidence"], workspace_root=self.root)
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

    def relative_workspace_path(self, resolved_path):
        try:
            return Path(resolved_path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None

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
