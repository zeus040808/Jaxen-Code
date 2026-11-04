"""Persistent semantic progress for the current user task.

RunState counts attempts and tools. TaskState answers the higher-level
questions: what is the goal, which explicit subtasks remain, what evidence was
collected, and which failures or claims are still unresolved.
"""

from __future__ import annotations

import re
import shlex
import uuid

from .workspace import clip, now


TASK_STATE_SCHEMA_VERSION = "v1"
EVIDENCE_LIMIT = 60
FAILURE_LIMIT = 30
CLAIM_LIMIT = 30
VERIFICATION_REPAIR_STEPS = 4
MAX_TODOS = 20
TODO_STATUSES = {"pending", "in_progress", "completed"}
CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h"}
CHECK_PREFIX = r"^(?:(?:uv|poetry)\s+run(?:\s+--with\s+\S+)*\s+)?"
TEST_COMMAND = re.compile(
    CHECK_PREFIX + r"(?:python(?:3(?:\.\d+)?)?\s+-m\s+(?:pytest|unittest)\b"
    r"|pytest\b|npm\s+(?:run\s+)?test\b|pnpm\s+test\b|yarn\s+test\b"
    r"|cargo\s+test\b|go\s+test\b|mvn\s+test\b|(?:\./)?gradle(?:w)?\s+test\b)",
    re.I,
)
STATIC_COMMAND = re.compile(
    CHECK_PREFIX + r"(?:python(?:3(?:\.\d+)?)?\s+-m\s+compileall\b"
    r"|ruff\s+check\b|mypy\b|tsc\b|npm\s+run\s+build\b|cargo\s+check\b)",
    re.I,
)


LEADING_CD = re.compile(r"^(?:cd\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s*&&\s*)+")


def superseded_by_progress(failure):
    """只读工具出错（读了不存在的路径、搜索参数不对、搜索的文件不存在）：之后任意一次工具调用成功就算已解决。

    按"同一动作后来成功才算解决"，错误的参数永远不会成功，这次失败会一直挂到交付
    （SWE-bench django-11265：读错路径后改对、改完代码、测试通过，仍报 incomplete_delivery；
    sphinx-8638：search 一个不存在的文件是运行时出错 tool_failed，同样一直挂到交付）。
    但失败之后一次成功都没有就说"完成"，仍然拦下。修改类工具和会改东西的 shell 命令不适用：
    patch_file 找不到原文、测试命令失败，那是该做的事没做成，必须由同一个动作成功来解决。
    """
    return failure.get("error_code") in {"invalid_arguments", "tool_failed"} and bool(failure.get("read_only"))


def _check_kind(command):
    command = str(command).strip()
    # 开头的 `cd 目录 &&` 是安全的：&& 会把失败传下去，退出码由后面的检查命令决定。
    command = LEADING_CD.sub("", command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        parts = list(lexer)
    except ValueError:
        return ""
    # Shell chaining can hide a failing test; punctuation inside quoted Python code is safe.
    if any(part and set(part) <= set(";&|<>") for part in parts):
        return ""
    if any(part in {"--help", "--version", "--collect-only", "--dry-run", "--listTests", "-h", "-V"} for part in parts):
        return ""
    if len(parts) >= 3 and re.fullmatch(r"python(?:3(?:\.\d+)?)?", parts[0], re.I):
        if parts[1] == "-c" and re.search(r"\bassert\b", parts[2]):
            return "test"
    if len(parts) >= 4 and parts[0] == "test" and "=" in parts:
        return "test"
    if TEST_COMMAND.match(command):
        return "test"
    if STATIC_COMMAND.match(command):
        return "static"
    return ""


def default_task_state():
    return {
        "schema_version": TASK_STATE_SCHEMA_VERSION,
        "task_id": "",
        "goal": "",
        "status": "idle",
        "run_ids": [],
        "continuation_requests": [],
        "subtask_ids": [],
        "evidence": [],
        "claims": [],
        "open_questions": [],
        "failures": [],
        "verification_pending": False,
        "verification": {"edit_revision": 0, "changed_paths": [], "checks": []},
        "next_action": "",
        # 会话内的轻量计划（todo_write 整体替换）；新任务时随 _reset 清空。
        "todos": [],
        "created_at": "",
        "updated_at": now(),
    }


class TaskState:
    CONTINUABLE_STATUSES = {"running", "waiting_for_user", "paused"}

    def __init__(self, state, task_store):
        self.state = state if isinstance(state, dict) else default_task_state()
        self.task_store = task_store
        self._ensure_shape()

    def _ensure_shape(self):
        defaults = default_task_state()
        for key, value in defaults.items():
            if key not in self.state:
                self.state[key] = list(value) if isinstance(value, list) else value
        for key in (
            "run_ids",
            "continuation_requests",
            "subtask_ids",
            "evidence",
            "claims",
            "open_questions",
            "failures",
        ):
            if not isinstance(self.state.get(key), list):
                self.state[key] = []
        if not isinstance(self.state.get("verification"), dict):
            self.state["verification"] = defaults["verification"]
        for key, value in defaults["verification"].items():
            self.state["verification"].setdefault(key, list(value) if isinstance(value, list) else value)
        if self.state["verification_pending"] and not self.state["verification"]["edit_revision"]:
            # Sessions created before detailed verification records still need a post-edit test.
            self.state["verification"]["edit_revision"] = 1
        self.state["schema_version"] = TASK_STATE_SCHEMA_VERSION

    def to_dict(self):
        return self.state

    def _touch(self):
        self.state["updated_at"] = now()

    def _reset(self, goal, run_id):
        self.state.clear()
        self.state.update(default_task_state())
        self.state.update(
            {
                "task_id": "taskrun_" + uuid.uuid4().hex[:10],
                "goal": clip(str(goal).strip(), 1000),
                "status": "running",
                "run_ids": [str(run_id)],
                "created_at": now(),
                "updated_at": now(),
            }
        )

    def begin(self, user_message, run_id):
        user_message = str(user_message).strip()
        if self.state.get("status") not in self.CONTINUABLE_STATUSES or not self.state.get("goal"):
            self._reset(user_message, run_id)
            return

        if user_message and user_message != self.state.get("goal"):
            self.state["continuation_requests"].append(clip(user_message, 500))
            self.state["continuation_requests"] = self.state["continuation_requests"][-10:]
        self.state["status"] = "running"
        self.state["run_ids"].append(str(run_id))
        self.state["run_ids"] = list(dict.fromkeys(self.state["run_ids"]))[-20:]
        if self.state["open_questions"]:
            self._append_evidence(
                {
                    "kind": "user_followup",
                    "summary": clip(user_message, 300),
                    "created_at": now(),
                }
            )
            self.state["open_questions"] = []
        self._touch()

    def _append_evidence(self, evidence):
        self.state["evidence"].append(evidence)
        self.state["evidence"] = self.state["evidence"][-EVIDENCE_LIMIT:]

    @staticmethod
    def _task_id_from_result(result):
        match = re.search(r"\b(task_[A-Za-z0-9_]+)\b", str(result))
        return match.group(1) if match else ""

    def observe_tool(self, name, args, result, metadata):
        metadata = dict(metadata or {})
        status = str(metadata.get("tool_status", "unknown"))
        error_code = str(metadata.get("tool_error_code", ""))
        task_id = str((args or {}).get("task_id", "")).strip()
        if name == "create_task" and status == "ok":
            task_id = self._task_id_from_result(result)
        if task_id and task_id not in self.state["subtask_ids"]:
            self.state["subtask_ids"].append(task_id)

        self._append_evidence(
            {
                "kind": "tool",
                "tool": str(name),
                "status": status,
                "error_code": error_code,
                "summary": clip(str(result), 350),
                "affected_paths": list(metadata.get("affected_paths", [])),
                "created_at": now(),
            }
        )

        verification = self.state["verification"]
        changed_code_paths = [
            str(path) for path in metadata.get("affected_paths", [])
            if str(path).lower().endswith(tuple(CODE_SUFFIXES))
        ] if metadata.get("workspace_changed") else []
        if changed_code_paths:
            verification["edit_revision"] += 1
            verification["changed_paths"] = sorted(set(verification["changed_paths"] + changed_code_paths))
            self.state["verification_pending"] = True
        elif name == "run_shell" and not (args or {}).get("background"):
            command = str((args or {}).get("command", ""))
            kind = _check_kind(command)
            if kind:
                passed = status == "ok" and not (
                    kind == "test" and re.search(r"Ran 0 tests\b", str(result), re.I)
                )
                verification["checks"].append({
                    "command": clip(command, 300),
                    "kind": kind,
                    "passed": passed,
                    "edit_revision": verification["edit_revision"],
                    "changed_paths": list(verification["changed_paths"]),
                    "summary": clip(str(result), 350),
                    "created_at": now(),
                })
                verification["checks"] = verification["checks"][-8:]
                if kind == "test" and verification["edit_revision"]:
                    self.state["verification_pending"] = not passed

        if status == "ok":
            for failure in self.state["failures"]:
                same_action = failure.get("tool") == name and (
                    not failure.get("path") or failure.get("path") == str((args or {}).get("path", ""))
                )
                if name == "run_shell" and failure.get("check_kind"):
                    same_action = same_action and failure["check_kind"] == _check_kind((args or {}).get("command", ""))
                if failure.get("retryable") and not failure.get("resolved") and (same_action or superseded_by_progress(failure)):
                    failure["resolved"] = True
                    failure["resolved_at"] = now()
                    failure["resolution"] = f"corrected by successful {name}"
            self.state["next_action"] = "Review the evidence and choose the next required action."
        else:
            fingerprint = str(metadata.get("failure_fingerprint", ""))
            self.state["failures"].append(
                {
                    "tool": str(name),
                    "path": str((args or {}).get("path", "")),
                    "check_kind": _check_kind((args or {}).get("command", "")) if name == "run_shell" else "",
                    "error_code": error_code or status,
                    "fingerprint": fingerprint,
                    "retryable": bool(metadata.get("retryable", False)),
                    "read_only": bool(metadata.get("read_only", False)),
                    "resolved": False,
                    "summary": clip(str(result), 350),
                    "created_at": now(),
                }
            )
            self.state["failures"] = self.state["failures"][-FAILURE_LIMIT:]
            self.state["next_action"] = str(metadata.get("recommended_action", "")).strip()
        self._touch()

    def record_claim(self, claim, evidence, verified=True, task_id=""):
        claim_text = clip(str(claim).strip(), 500)
        task_id = str(task_id).strip()
        if verified:
            for previous in reversed(self.state["claims"]):
                if previous.get("claim") == claim_text and previous.get("task_id", "") == task_id:
                    previous["evidence"] = clip(str(evidence).strip(), 700)
                    previous["verified"] = True
                    previous["verified_at"] = now()
                    self._append_evidence(
                        {
                            "kind": "claim",
                            "claim": claim_text,
                            "summary": previous["evidence"],
                            "verified": True,
                            "task_id": task_id,
                            "created_at": now(),
                        }
                    )
                    self._touch()
                    return "updated claim as verified"
        item = {
            "claim": claim_text,
            "evidence": clip(str(evidence).strip(), 700),
            "verified": bool(verified),
            "task_id": task_id,
            "created_at": now(),
        }
        self.state["claims"].append(item)
        self.state["claims"] = self.state["claims"][-CLAIM_LIMIT:]
        if item["task_id"] and item["task_id"] not in self.state["subtask_ids"]:
            self.state["subtask_ids"].append(item["task_id"])
        self._append_evidence({"kind": "claim", "summary": item["evidence"], **item})
        self._touch()
        return "recorded verified claim" if verified else "recorded unverified claim"

    def record_background_notification(self, notification):
        self._append_evidence(
            {
                "kind": "background",
                "summary": clip(str(notification), 500),
                "created_at": now(),
            }
        )
        self._touch()

    def mark_waiting(self, question):
        self.state["status"] = "waiting_for_user"
        question = clip(str(question).strip(), 500)
        if question and question not in self.state["open_questions"]:
            self.state["open_questions"].append(question)
        self.state["next_action"] = question
        self._touch()

    def mark_paused(self, reason, next_action=""):
        self.state["status"] = "paused"
        self.state["next_action"] = clip(str(next_action or reason), 500)
        self._touch()

    def mark_blocked(self, reason):
        self.state["status"] = "blocked"
        self.state["next_action"] = clip(str(reason), 500)
        self._touch()

    def mark_completed(self, final_answer):
        self.state["status"] = "completed"
        self.state["next_action"] = ""
        self._append_evidence(
            {"kind": "delivery", "summary": clip(str(final_answer), 500), "created_at": now()}
        )
        self._touch()

    def _subtasks(self):
        tasks = []
        for task_id in self.state["subtask_ids"]:
            try:
                tasks.append(self.task_store.load(task_id))
            except (FileNotFoundError, ValueError, TypeError):
                continue
        return tasks

    def delivery_blockers(self, background_pending=0):
        blockers = []
        # 改了代码但没跑通测试只是提示（见 render 的 Code verification 一行），不阻止交付：
        # 正则识别不了所有合法验证方式（复现脚本、仓库自定义命令），硬门禁会误报，
        # 还会逼模型自己写测试文件来过关。
        if background_pending:
            blockers.append(f"{background_pending} background task(s) are still running")

        for task in self._subtasks():
            if task.status in {"pending", "in_progress"}:
                blockers.append(f"subtask {task.id} is {task.status}")
            elif task.status in {"failed", "blocked"}:
                reason = task.failure_reason or "no reason recorded"
                blockers.append(f"subtask {task.id} is {task.status}: {reason}")

        for claim in self.state["claims"]:
            if not claim.get("verified"):
                blockers.append(f"claim is not verified: {claim.get('claim', '')}")

        for question in self.state["open_questions"]:
            blockers.append(f"open question: {question}")

        for failure in self.state["failures"]:
            if not failure.get("resolved") and failure.get("retryable"):
                blockers.append(
                    f"unresolved failure {failure.get('tool')}:{failure.get('error_code')}"
                )
        return list(dict.fromkeys(blockers))

    def completed_summaries(self):
        return [f"{task.id}: {task.subject}" for task in self._subtasks() if task.status == "completed"]

    def blocker_summary(self):
        blockers = self.delivery_blockers()
        return " | ".join(blockers[:4])

    def set_todos(self, todos):
        """Replace the whole todo list; the model always restates the full plan."""
        if not isinstance(todos, list) or len(todos) > MAX_TODOS:
            raise ValueError(f"todos must be a list of at most {MAX_TODOS} items")
        cleaned = []
        for item in todos:
            if not isinstance(item, dict):
                raise ValueError("each todo must be an object with content and status")
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).strip()
            if not content:
                raise ValueError("todo content must not be empty")
            if status not in TODO_STATUSES:
                raise ValueError(f"todo status must be one of {sorted(TODO_STATUSES)}")
            cleaned.append({"content": clip(content, 200), "status": status})
        self.state["todos"] = cleaned
        self._touch()
        return self.render_todos() or "Todo list cleared."

    def render_todos(self):
        marks = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
        return "\n".join(
            f"  {marks[item['status']]} {item['content']}" for item in self.state.get("todos", [])
        )

    def render(self):
        lines = [
            "Task progress:",
            f"- Goal: {self.state.get('goal') or '-'}",
            f"- Status: {self.state.get('status') or 'idle'}",
            f"- Next action: {self.state.get('next_action') or '-'}",
        ]
        todos = self.render_todos()
        if todos:
            lines.append("- Todos:")
            lines.append(todos)
        verification = self.state["verification"]
        if verification["edit_revision"]:
            lines.append(
                f"- Code verification: {'pending test' if self.state['verification_pending'] else 'tested'} "
                f"for edit {verification['edit_revision']} ({', '.join(verification['changed_paths'])})"
            )
            if verification["checks"]:
                check = verification["checks"][-1]
                lines.append(
                    f"- Last check: {check['kind']} {'passed' if check['passed'] else 'failed'} "
                    f"on edit {check['edit_revision']}: {check['command']}"
                )
        tasks = self._subtasks()
        if tasks:
            lines.append("- Subtasks:")
            for task in tasks[-12:]:
                reason = f" reason={task.failure_reason}" if task.failure_reason else ""
                lines.append(f"  - {task.id}: {task.subject} [{task.status}]{reason}")
        else:
            lines.append("- Subtasks: none recorded")

        unresolved = [item for item in self.state["failures"] if not item.get("resolved")]
        lines.append(
            "- Unresolved failures: "
            + (" | ".join(f"{item.get('tool')}:{item.get('error_code')}" for item in unresolved[-4:]) or "none")
        )
        unverified = [item.get("claim", "") for item in self.state["claims"] if not item.get("verified")]
        lines.append("- Unverified claims: " + (" | ".join(unverified[-4:]) or "none"))
        lines.append(
            "- Open questions: " + (" | ".join(self.state["open_questions"][-4:]) or "none")
        )
        recent_evidence = self.state["evidence"][-5:]
        lines.append("- Recent evidence:")
        if recent_evidence:
            lines.extend(f"  - {item.get('kind')}: {item.get('summary', '')}" for item in recent_evidence)
        else:
            lines.append("  - none")
        return "\n".join(lines)
