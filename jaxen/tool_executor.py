"""工具执行闸口：校验、审批、执行和结果归一化。

AgentLoop 决定何时调用工具；本模块决定一次调用是否允许及如何执行。
"""

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import re
from pathlib import Path

from .progress import _check_kind, superseded_by_progress
from .workspace import clip_head_tail, file_freshness


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict

    def prompt_content(self):
        metadata = self.metadata or {}
        outcome = {
            "status": metadata.get("tool_status", "unknown"),
            "error_code": metadata.get("tool_error_code", ""),
            "retryable": bool(metadata.get("retryable", False)),
            "recommended_action": metadata.get("recommended_action", ""),
            "failure_repeat_count": int(metadata.get("failure_repeat_count", 0) or 0),
        }
        return (
            f"<tool_result>{self.content}</tool_result>\n"
            f"<tool_outcome>{json.dumps(outcome, ensure_ascii=False, sort_keys=True)}</tool_outcome>"
        )


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint="",
    diff_summary=None,
    retryable=False,
    recommended_action="",
    stop_run=False,
    out_of_scope_paths=None,
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "read_only": read_only,
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
        "retryable": bool(retryable),
        "recommended_action": str(recommended_action),
        "stop_run": bool(stop_run),
        # 越出允许写入范围的路径。空列表是常态，非空即安全事件。
        "out_of_scope_paths": list(out_of_scope_paths or []),
    }
    if workspace_fingerprint:
        result["workspace_fingerprint"] = workspace_fingerprint
    return result


class ToolExecutor:
    # 可以并行执行调用的函数名单
    PARALLEL_READ_TOOLS = frozenset({"list_files", "read_file", "read_files", "search", "glob", "repo_map", "runtime_info"})

    # 防止模型凭记忆或过期内容改文件：
    # - write_file 覆盖已有文件前必须先读过（整文件盲写风险最大）；
    # - patch_file 的 old_text 必须唯一匹配，本身证明了模型知道内容，不强制先读；
    # - 任何编辑在「读过之后文件又被改了」时都拒绝。新建文件不受限。
    EDIT_TOOLS = frozenset({"write_file", "patch_file"})

    def __init__(self, agent):
        self.agent = agent
        # 绝对路径 -> 模型最近一次看到的内容指纹；跨 run 保留，Session 内有效。
        self._observed_files = {}
        self.reset_run_state()

    def execute(self, name, args):
        name = self.agent.canonical_tool_name(name)
        # 得到工具调用结果
        result = self._execute(name, args)
        return self._observe_result(name, args, result)

    def execute_read_batch(self, actions):
        """Run independent reads concurrently; update shared state in call order."""
        if not 2 <= len(actions) <= 4:
            raise ValueError("parallel read batch must contain 2 to 4 calls")
        prepared = []
        for action in actions:
            name = self.agent.canonical_tool_name(action["name"])
            args = action["args"]
            if name not in self.PARALLEL_READ_TOOLS or name not in self.agent.tools:
                raise ValueError("parallel batches support read-only tools only")
            if self.agent.allowed_tools is not None and name not in self.agent.allowed_tools:
                raise ValueError(f"tool '{name}' is not allowed in this run")
            self.agent.validate_tool(name, args)
            prepared.append((name, args, self.agent.tools[name]["run"]))

        def run_one(item):
            name, args, runner = item
            try:
                content = self.agent.redact_text(clip_head_tail(runner(args)))
                return ToolExecutionResult(content, _metadata("ok", read_only=True))
            except Exception as exc:
                return ToolExecutionResult(
                    f"error: tool {name} failed: {self.agent.redact_text(str(exc))}",
                    _metadata("error", tool_error_code="tool_failed", read_only=True,
                              retryable=True, recommended_action="Inspect the error and retry with corrected arguments."),
                )

        with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
            results = list(pool.map(run_one, prepared))
        observed = []
        for (name, args, _), result in zip(prepared, results):
            if result.metadata["tool_status"] == "ok":
                self.agent.update_memory_after_tool(name, args, result.content)
                self._remember_files(name, args)
            result = self._observe_result(name, args, result)
            self.agent.progress.observe_tool(name, args, result.content, result.metadata)
            observed.append(result)
        self.agent.session["progress"] = self.agent.progress.to_dict()
        self.agent.session_path = self.agent.session_store.save(self.agent.session)
        return observed

    def execute_approved(self, name, args, expected_fingerprint):
        """Execute exactly one previously persisted approval request."""

        name = self.agent.canonical_tool_name(name)
        if self._call_fingerprint(name, args) != str(expected_fingerprint):
            raise ValueError("approved action fingerprint mismatch")
        result = self._execute(name, args, approval_granted=True)
        return self._observe_result(name, args, result)

    def reset_run_state(self):
        self._failure_counts = {}
        self._unresolved_failure = None
        self._read_cache = {}
        self._read_cache_hits = {}
        self._successful_calls = set()

    def completion_blocker(self):
        return dict(self._unresolved_failure or {})

    def _file_key(self, raw_path):
        return str((Path(self.agent.root) / str(raw_path)).resolve())

    def _remember_files(self, name, args):
        if name == "read_file" or name in self.EDIT_TOOLS:
            raw_paths = [(args or {}).get("path")]
        elif name == "read_files":
            raw_paths = list((args or {}).get("paths") or [])
        else:
            return
        for raw_path in raw_paths:
            digest = file_freshness(raw_path, self.agent.root)
            if digest:
                self._observed_files[self._file_key(raw_path)] = digest

    def _stale_edit_reason(self, name, args):
        """Why an edit must read the file first, or '' when it may proceed."""
        if name not in self.EDIT_TOOLS:
            return ""
        raw_path = str((args or {}).get("path", "")).strip()
        current = file_freshness(raw_path, self.agent.root)
        if not current:
            return ""
        seen = self._observed_files.get(self._file_key(raw_path))
        if seen is None:
            return "file_not_read" if name == "write_file" else ""
        return "" if seen == current else "file_changed_since_read"

    # 只有这两个工具在调用前就能确定写入目标。run_shell 的目标要执行后
    # 才知道，由下面的 workspace diff 兜底。
    WRITE_TARGET_ARGS = {"write_file": "path", "patch_file": "path"}

    @classmethod
    def _declared_write_targets(cls, name, args):
        key = cls.WRITE_TARGET_ARGS.get(name)
        if not key:
            return []
        target = str((args or {}).get(key, "")).strip()
        return [target] if target else []

    @classmethod
    def _normalized_value(cls, value):
        if isinstance(value, dict):
            return {str(key): cls._normalized_value(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [cls._normalized_value(item) for item in value]
        if isinstance(value, str):
            return " ".join(value.split())
        return value

    @classmethod
    def _failure_fingerprint(cls, name, args, error_code):
        payload = {
            "name": str(name).strip(),
            "args": cls._normalized_value(args or {}),
            "error_code": str(error_code).strip(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _call_fingerprint(cls, name, args):
        payload = {"name": str(name).strip(), "args": cls._normalized_value(args or {})}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _read_freshness(self, name, args):
        if name not in {"read_file", "read_files"}:
            return ""
        raw_paths = [args.get("path")] if name == "read_file" else list(args.get("paths") or [])
        digests = [file_freshness(raw_path, self.agent.root) for raw_path in raw_paths]
        if not digests or any(not digest for digest in digests):
            return ""
        payload = list(zip((str(path) for path in raw_paths), digests))
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _read_cache_key(self, name, args):
        freshness = self._read_freshness(name, args)
        if not freshness:
            return ""
        return self._call_fingerprint(name, args) + ":" + freshness

    def _observe_result(self, name, args, result):
        metadata = dict(result.metadata or {})
        status = str(metadata.get("tool_status", ""))
        if status in {"ok", "waiting", "denied"}:
            previous = self._unresolved_failure
            if previous and status == "ok" and superseded_by_progress(previous):
                self._unresolved_failure = None  # 只读工具出错：之后有调用成功就算绕过去了
            elif previous and status == "ok" and previous.get("name") == name:
                previous_path = previous.get("path")
                same_check = not previous.get("check_kind") or previous["check_kind"] == _check_kind((args or {}).get("command", ""))
                if same_check and (not previous_path or previous_path == str((args or {}).get("path", ""))):
                    self._unresolved_failure = None
            return result

        error_code = str(metadata.get("tool_error_code", "") or status or "tool_failed")
        fingerprint = self._failure_fingerprint(name, args, error_code)
        count = self._failure_counts.get(fingerprint, 0) + 1
        self._failure_counts[fingerprint] = count
        metadata["failure_fingerprint"] = fingerprint
        metadata["failure_repeat_count"] = count

        if count >= 2 and bool(metadata.get("retryable", False)):
            metadata.update(
                {
                    "tool_status": "rejected",
                    "tool_error_code": "repeated_failed_call",
                    "retryable": False,
                    "recommended_action": "Do not retry this failed call again; explain the blocker or choose a materially different action.",
                    "stop_run": True,
                }
            )
            result = ToolExecutionResult(
                content=(
                    f"error: repeated failed tool call for {name}; previous error was {error_code}. "
                    "The run is stopping to prevent a loop."
                ),
                metadata=metadata,
            )
        else:
            result = ToolExecutionResult(content=result.content, metadata=metadata)

        self._unresolved_failure = {
            "name": name,
            "path": str((args or {}).get("path", "")),
            "check_kind": _check_kind((args or {}).get("command", "")) if name == "run_shell" else "",
            "error_code": metadata.get("tool_error_code", error_code),
            "retryable": bool(metadata.get("retryable", False)),
            "read_only": bool(metadata.get("read_only", False)),
            "recommended_action": metadata.get("recommended_action", ""),
        }
        return result

    # 核心工具调用函数
    def _execute(self, name, args, approval_granted=False):
        agent = self.agent
        name = agent.canonical_tool_name(name)
        args = args or {}
        # 检查是否允许调用工具
        if agent.allowed_tools is not None and name not in agent.allowed_tools:
            return ToolExecutionResult(
                content=f"error: tool '{name}' is not allowed in this run",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="tool_not_allowed",
                    risk_level="high",
                    read_only=False,
                    retryable=False,
                    recommended_action="Return a blocked answer because this tool is unavailable in the current run.",
                    stop_run=True,
                ),
            )

        # 检查工具是否存在
        tool = agent.tools.get(name)
        if tool is None:
            return ToolExecutionResult(
                content=f"error: unknown tool '{name}'",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="unknown_tool",
                    risk_level="high",
                    read_only=False,
                    # 同名重试必然再失败，也就永远不会被"同名工具成功"标为已解决；
                    # 若记为可重试，会一直挂在交付阻塞里（SWE-bench django-11138 误报 incomplete_delivery）。
                    retryable=False,
                    recommended_action="Choose one of the tools listed in the prompt.",
                ),
            )

        try:
            # 进行工具参数校验
            agent.validate_tool(name, args)
        except Exception as exc:
            # 参数校验失败时给出example
            example = agent.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            # 路径逃脱
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            path_escape = bool(security_event_type)
            return ToolExecutionResult(
                content=message,
                metadata=_metadata(
                    "rejected",
                    tool_error_code="path_escape" if path_escape else "invalid_arguments",
                    security_event_type=security_event_type,
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                    retryable=not path_escape,
                    recommended_action=(
                        "Do not retry an out-of-workspace path; choose a safe in-workspace action or return a blocked answer."
                        if path_escape else
                        "Correct the arguments using the provided example before retrying."
                    ),
                    stop_run=False,
                ),
            )

        call_fingerprint = self._call_fingerprint(name, args)
        cache_key = self._read_cache_key(name, args)
        if cache_key and cache_key in self._read_cache:
            cached = self._read_cache[cache_key]
            cache_hit_count = self._read_cache_hits.get(cache_key, 0) + 1
            self._read_cache_hits[cache_key] = cache_hit_count
            raw_paths = [args.get("path")] if name == "read_file" else list(args.get("paths") or [])
            summaries = []
            for raw_path in raw_paths:
                summary = agent.source_evidence.file_summary(raw_path)
                if summary:
                    summaries.append(f"- {raw_path}: {summary}")
            content = (
                f"cached observation: unchanged successful {name} result reused; no tool step consumed.\n"
                + ("\n".join(summaries) if summaries else "Use the existing verified evidence.")
            )
            self._remember_files(name, args)
            metadata = dict(cached.metadata)
            metadata.update(
                {
                    "tool_status": "ok",
                    "cache_hit": True,
                    "cache_hit_count": cache_hit_count,
                    "effective_tool_step": False,
                    "retryable": False,
                    "recommended_action": "Use the existing source evidence; do not repeat this unchanged read again.",
                }
            )
            return ToolExecutionResult(content=content, metadata=metadata)

        # Read tools use the freshness-aware cache above. Other tools retain the
        # legacy consecutive-loop guard because their surrounding state may have
        # changed even when arguments are identical (for example backlog_complete).
        # 缓存是否命中、是否重复调用
        if not cache_key and agent.repeated_tool_call(name, args):
            return ToolExecutionResult(
                content=f"error: repeated identical tool call for {name}; choose a different tool or return a final answer",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="repeated_identical_call",
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                    retryable=True,
                    recommended_action="Choose a materially different action or return a blocked/final answer.",
                    stop_run=False,
                ),
            )

        # 写入范围是一条硬护栏，和路径逃逸同级：越界的写不该走到审批，
        # 更不该真的落盘。提示词里的"只改这些文件"约束不住模型，这里能。
        scope_violations = agent.write_scope_violations(
            self._declared_write_targets(name, args)
        )
        if scope_violations:
            return ToolExecutionResult(
                content=(
                    f"error: {name} targets a path outside the allowed write scope: "
                    + ", ".join(scope_violations)
                    + f"; allowed: {', '.join(agent.write_scope)}"
                ),
                metadata=_metadata(
                    "rejected",
                    tool_error_code="write_scope_violation",
                    security_event_type="write_scope_violation",
                    risk_level="high",
                    read_only=False,
                    retryable=False,
                    recommended_action=(
                        "Restrict the change to the allowed paths, or return a blocked "
                        "answer explaining why the task needs a wider scope."
                    ),
                    affected_paths=scope_violations,
                    stop_run=False,
                ),
            )

        stale_reason = self._stale_edit_reason(name, args)
        if stale_reason:
            target = str(args.get("path", ""))
            problem = (
                "has not been read in this session"
                if stale_reason == "file_not_read"
                else "changed on disk since it was last read"
            )
            return ToolExecutionResult(
                content=f"error: {name} refused: {target} {problem}. Read it with read_file first, then retry.",
                metadata=_metadata(
                    "rejected",
                    tool_error_code=stale_reason,
                    risk_level="high",
                    read_only=False,
                    retryable=True,
                    recommended_action=f"Call read_file on {target} and base the edit on its current content.",
                    stop_run=False,
                ),
            )

        # 检查风险工具调用是否被批准
        requires_approval = agent.tool_requires_approval(name, args)
        approval = True if approval_granted else agent.approve(name, args) if requires_approval else True
        if requires_approval and approval is None:
            pending = dict(agent.session.get("pending_input", {}) or {})
            metadata = _metadata(
                "waiting",
                tool_error_code="approval_required",
                security_event_type="approval_requested",
                risk_level="high",
                read_only=False,
                retryable=False,
                recommended_action="Wait for an explicit structured approval response.",
            )
            metadata.update(
                {
                    "effective_tool_step": False,
                    "input_request": pending,
                }
            )
            return ToolExecutionResult(
                content=f"approval required before {name}; no workspace action was executed",
                metadata=metadata,
            )
        if requires_approval and not approval:
            return ToolExecutionResult(
                content=f"error: approval denied for {name}",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="approval_denied",
                    security_event_type="read_only_block" if agent.read_only else "approval_denied",
                    risk_level="high",
                    read_only=False,
                    retryable=False,
                    recommended_action="Return a blocked answer and explain that approval or write access is required.",
                    stop_run=True,
                ),
            )
        # 危险工具执行前拍 workspace 快照
        before_snapshot = agent.capture_workspace_snapshot() if requires_approval else {}
        after_snapshot = before_snapshot

        try:
            # 执行工具（获取工具调用结果的时候先做一次压缩截断处理）
            content = agent.redact_text(clip_head_tail(tool["run"](args)))
            after_snapshot = agent.capture_workspace_snapshot() if requires_approval else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", content)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            # run_shell 之类的工具执行完才知道动了哪些文件。事后拦不住写入，
            # 但必须把越界如实报出来并停下，而不是让它悄悄通过。
            escaped_writes = agent.write_scope_violations(affected_paths)
            if escaped_writes:
                tool_status = "rejected"
                tool_error_code = "write_scope_violation"

            agent.update_memory_after_tool(name, args, content)
            metadata = _metadata(
                tool_status,
                tool_error_code=tool_error_code,
                security_event_type="write_scope_violation" if escaped_writes else "",
                risk_level="high" if requires_approval else "low",
                read_only=not requires_approval,
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
                out_of_scope_paths=escaped_writes,
                retryable=tool_status in {"error", "partial_success"},
                recommended_action=(
                    "Revert the out-of-scope changes to "
                    + ", ".join(escaped_writes)
                    + " and keep edits inside the allowed paths."
                    if escaped_writes
                    else "Inspect the output and workspace diff, then choose a corrected action."
                    if tool_status in {"error", "partial_success"} else ""
                ),
                stop_run=bool(escaped_writes),
            )
            result = ToolExecutionResult(content=content, metadata=metadata)
            # 真正执行过一次工具：重复读取的计数清零，只有原地连续重复才算死循环。
            # 工作区有改动时清空读缓存：改过（哪怕又撤回成原样）之后再读是正常工作，要返回真实内容
            # （django-12325：撤回后回头看代码只拿到"内容没变"的摘要，再读就被禁用了全部工具）。
            self._read_cache_hits.clear()
            if workspace_changed:
                self._read_cache.clear()
            if tool_status == "ok":
                self._successful_calls.add(call_fingerprint)
                self._remember_files(name, args)
                if cache_key:
                    self._read_cache[cache_key] = result
                # 成功改过文件之后，再跑同一条失败过的命令是"改完验证"，不是死循环：失败计数清零
                # （django-12754：改完代码重跑复现脚本，被判为重复失败，整个运行被停掉）。
                # 只认成功的改动：失败命令自己顺带写了文件（partial_success）不算进展，否则真循环永远停不下来。
                if workspace_changed:
                    self._failure_counts.clear()
            return result
        except Exception as exc:
            after_snapshot = agent.capture_workspace_snapshot() if requires_approval else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            metadata = _metadata(
                "partial_success" if workspace_changed else "error",
                tool_error_code="tool_partial_success" if workspace_changed else "tool_failed",
                security_event_type=security_event_type,
                risk_level="high" if requires_approval else "low",
                read_only=not requires_approval,
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
                retryable=True,
                recommended_action="Inspect the error and any workspace diff before choosing a corrected action.",
            )
            return ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)
