"""Main HH Coder Agent runtime."""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .background import BackgroundManager
from .context import ContextManager
from .cron import CronScheduler
from .security import ApprovalPolicy, redacted_json
from .state import RunState
from .stores import RunStore, SessionStore
from .task_system import TaskStore
from .tools import ToolResult, build_registry
from .workspace import Workspace, clip, now


class HHCoderAgent:
    def __init__(
        self,
        model_client,
        root: str | Path = ".",
        approval: str = "ask",
        max_steps: int = 8,
        max_tokens: int = 2048,
        session: dict | None = None,
        read_only: bool = False,
    ):
        self.workspace = Workspace(root)
        self.model_client = model_client
        self.approval = ApprovalPolicy(approval, read_only=read_only)
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.session_store = SessionStore(self.workspace.root)
        self.run_store = RunStore(self.workspace.root)
        self.session = session or self.session_store.new()
        self.tasks = TaskStore(self.workspace.root)
        self.cron = CronScheduler(self.workspace.root)
        self.background = BackgroundManager()
        self.tools = build_registry(self)
        self.context = ContextManager(self)
        self.last_run_state: RunState | None = None
        self.session_store.save(self.session)

    @staticmethod
    def new_run_id() -> str:
        return "run_" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def memory_note(self, text: str, tags: list[str] | None = None) -> None:
        memory = self.session.setdefault("memory", {"notes": [], "file_summaries": {}})
        note = text if not tags else f"{text} [{' '.join(tags)}]"
        notes = memory.setdefault("notes", [])
        if note in notes:
            notes.remove(note)
        notes.append(note)
        del notes[:-50]

    def record(self, item: dict) -> None:
        self.session.setdefault("history", []).append(item)
        self.session_store.save(self.session)

    def ask(self, user_message: str) -> str:
        state = RunState(run_id=self.new_run_id(), user_request=user_message)
        self.last_run_state = state
        self.record({"role": "user", "content": user_message, "created_at": now()})
        self.run_store.write_state(state)
        self.run_store.trace(state, "run_started", {"user_request": clip(user_message, 500)})

        for notification in self.background.collect():
            self.record({"role": "user", "content": notification, "created_at": now()})
        for job in self.cron.consume():
            self.record({"role": "user", "content": f"[Scheduled] {job.prompt}", "created_at": now()})

        malformed = 0
        while state.tool_steps < self.max_steps:
            state.record_attempt()
            prompt, metadata = self.context.build(user_message)
            self.run_store.trace(state, "prompt_built", metadata)
            raw = self.model_client.complete(prompt, max_tokens=self.max_tokens)
            self.run_store.trace(state, "model_completed", {
                "raw_preview": clip(raw, 800),
                "metadata": redacted_json(getattr(self.model_client, "last_metadata", {})),
            })
            kind, payload = self.parse(raw)
            self.run_store.trace(state, "model_parsed", {"kind": kind})

            if kind == "retry":
                malformed += 1
                notice = str(payload)
                self.record({"role": "assistant", "content": notice, "created_at": now()})
                if malformed >= 3:
                    state.stop("模型连续返回无法解析的动作，已停止。", "malformed_limit")
                    break
                continue

            if kind == "final":
                final = str(payload).strip()
                self.record({"role": "assistant", "content": final, "created_at": now()})
                state.finish(final)
                break

            name = str(payload.get("name", "")).strip()
            args = payload.get("args") or {}
            result = self.execute_tool(name, args)
            state.record_tool(name, args, result.status, clip(result.content, 500))
            self.record({
                "role": "tool",
                "name": name,
                "args": args,
                "content": result.content,
                "created_at": now(),
            })
            self.run_store.trace(state, "tool_executed", {
                "name": name,
                "args": redacted_json(args),
                "status": result.status,
                "result": clip(result.content, 700),
                "metadata": result.metadata or {},
            })
            self.run_store.write_state(state)

        if state.status == "running":
            state.stop("达到工具步数上限，已停止。", "step_limit")
            self.record({"role": "assistant", "content": state.final_answer, "created_at": now()})

        self.run_store.write_state(state)
        self.run_store.trace(state, "run_finished", {
            "status": state.status,
            "stop_reason": state.stop_reason,
            "final_answer": clip(state.final_answer, 1000),
        })
        self.run_store.report(state, self.build_report(state))
        self.session_store.save(self.session)
        return state.final_answer

    def execute_tool(self, name: str, args: dict) -> ToolResult:
        tool = self.tools.get(name)
        if not tool:
            return ToolResult(f"unknown tool: {name}", status="error")
        allowed, reason = self.approval.approve(name, args)
        if not allowed:
            return ToolResult(f"tool rejected: {reason}", status="rejected")
        if self.background.should_background(name, args):
            clean_args = {**args, "background": False}
            task_id = self.background.start(
                str(args.get("command", name)),
                lambda n=name, a=clean_args: self.execute_tool(n, a).content,
            )
            return ToolResult(f"background task {task_id} started")
        try:
            return tool.handler(args)
        except Exception as error:
            return ToolResult(f"tool error: {error}", status="error")

    def build_report(self, state: RunState) -> dict:
        return {
            "run_id": state.run_id,
            "status": state.status,
            "stop_reason": state.stop_reason,
            "final_answer": state.final_answer,
            "attempts": state.attempts,
            "tool_steps": state.tool_steps,
            "workspace": str(self.workspace.root),
            "artifacts": {
                "task_state": f".hh_coder/runs/{state.run_id}/task_state.json",
                "trace": f".hh_coder/runs/{state.run_id}/trace.jsonl",
                "report": f".hh_coder/runs/{state.run_id}/report.json",
            },
        }

    @staticmethod
    def parse(raw: str) -> tuple[str, object]:
        text = str(raw or "")
        if "<tool>" in text and ("<final>" not in text or text.find("<tool>") < text.find("<final>")):
            body = HHCoderAgent.extract(text, "tool")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return "retry", "Runtime notice: malformed JSON inside <tool>."
            if not isinstance(payload, dict) or not payload.get("name"):
                return "retry", "Runtime notice: tool payload needs name and args."
            if payload.get("args") is None:
                payload["args"] = {}
            if not isinstance(payload.get("args"), dict):
                return "retry", "Runtime notice: tool args must be an object."
            return "tool", payload
        if "<tool" in text and ("<final>" not in text or text.find("<tool") < text.find("<final>")):
            payload = HHCoderAgent.parse_xml_tool(text)
            if payload:
                return "tool", payload
            return "retry", "Runtime notice: malformed XML tool call."
        if "<final>" in text:
            final = HHCoderAgent.extract(text, "final").strip()
            return ("final", final) if final else ("retry", "Runtime notice: empty final answer.")
        return ("final", text.strip()) if text.strip() else ("retry", "Runtime notice: empty model response.")

    @staticmethod
    def parse_xml_tool(raw: str) -> dict | None:
        match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
        if not match:
            return None
        attrs = HHCoderAgent.parse_attrs(match.group("attrs"))
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None
        args = dict(attrs)
        body = match.group("body")
        for key in ("content", "old_text", "new_text", "command", "pattern", "path", "prompt"):
            if f"<{key}>" in body:
                args[key] = HHCoderAgent.extract_raw(body, key)
        if name == "write_file" and "content" not in args and body.strip():
            args["content"] = body.strip("\n")
        return {"name": name, "args": args}

    @staticmethod
    def parse_attrs(text: str) -> dict:
        attrs = {}
        for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
            attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return attrs

    @staticmethod
    def extract(text: str, tag: str) -> str:
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        return text[start:end if end != -1 else None].strip()

    @staticmethod
    def extract_raw(text: str, tag: str) -> str:
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return ""
        start += len(start_tag)
        end = text.find(end_tag, start)
        return text[start:end if end != -1 else None]

