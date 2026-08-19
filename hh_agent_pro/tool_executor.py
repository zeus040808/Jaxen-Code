"""Structured tool execution for the agent runtime."""

from dataclasses import dataclass
import hashlib
import json
import re

from .workspace import clip, file_freshness


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
    def __init__(self, agent):
        self.agent = agent
        self.reset_run_state()

    def execute(self, name, args):
        name = self.agent.canonical_tool_name(name)
        result = self._execute(name, args)
        return self._observe_result(name, args, result)

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
            "error_code": metadata.get("tool_error_code", error_code),
            "retryable": bool(metadata.get("retryable", False)),
            "recommended_action": metadata.get("recommended_action", ""),
        }
        return result

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
                    retryable=True,
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
        # changed even when arguments are identical (for example complete_task).
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
            # 执行工具
            content = agent.redact_text(clip(tool["run"](args)))

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
            if tool_status == "ok":
                self._successful_calls.add(call_fingerprint)
                if cache_key:
                    self._read_cache[cache_key] = result
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
