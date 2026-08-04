"""Tool registry and implementations."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from .security import shell_env
from .workspace import Workspace, clip


@dataclass
class ToolResult:
    content: str
    status: str = "ok"
    metadata: dict | None = None


@dataclass
class Tool:
    name: str
    description: str
    args: dict
    handler: object
    mutates: bool = False

    def signature(self) -> str:
        return f"- {self.name}: {self.description}. args={json.dumps(self.args, ensure_ascii=False)}"


class ToolKit:
    def __init__(self, agent):
        self.agent = agent
        self.workspace: Workspace = agent.workspace

    def list_files(self, args: dict) -> ToolResult:
        limit = int(args.get("limit", 200))
        return ToolResult("\n".join(self.workspace.iter_files(limit=limit)) or "(no files)")

    def read_file(self, args: dict) -> ToolResult:
        path = self.workspace.path(args["path"])
        limit = int(args.get("limit", 4000))
        text = path.read_text(encoding="utf-8", errors="replace")
        self.agent.memory_note(f"read {self.workspace.rel(path)}", tags=[self.workspace.rel(path)])
        return ToolResult(clip(text, limit), metadata={"affected_paths": [self.workspace.rel(path)]})

    def write_file(self, args: dict) -> ToolResult:
        path = self.workspace.path(args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(args.get("content", ""))
        path.write_text(content, encoding="utf-8")
        return ToolResult(f"wrote {self.workspace.rel(path)} ({len(content)} chars)",
                          metadata={"affected_paths": [self.workspace.rel(path)]})

    def patch_file(self, args: dict) -> ToolResult:
        path = self.workspace.path(args["path"])
        old = str(args.get("old_text", ""))
        new = str(args.get("new_text", ""))
        text = path.read_text(encoding="utf-8", errors="replace")
        if not old:
            return ToolResult("old_text cannot be empty", status="error")
        if old not in text:
            return ToolResult(f"old_text not found in {self.workspace.rel(path)}", status="error")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return ToolResult(f"patched {self.workspace.rel(path)}", metadata={"affected_paths": [self.workspace.rel(path)]})

    def search(self, args: dict) -> ToolResult:
        pattern = str(args["pattern"])
        regex = re.compile(pattern)
        limit = int(args.get("limit", 100))
        lines = []
        for rel in self.workspace.iter_files(limit=1000):
            path = self.workspace.path(rel)
            try:
                for idx, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if regex.search(line):
                        lines.append(f"{rel}:{idx}: {line}")
                        if len(lines) >= limit:
                            return ToolResult("\n".join(lines))
            except OSError:
                continue
        return ToolResult("\n".join(lines) if lines else "(no matches)")

    def run_shell(self, args: dict) -> ToolResult:
        command = str(args["command"])
        timeout = int(args.get("timeout", 120))
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.workspace.root,
                env=shell_env(self.workspace.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            status = "ok" if proc.returncode == 0 else "error"
            return ToolResult(clip(output or "(no output)", 50_000), status=status,
                              metadata={"exit_code": proc.returncode})
        except subprocess.TimeoutExpired:
            return ToolResult(f"command timed out after {timeout}s", status="error")

    def todo_write(self, args: dict) -> ToolResult:
        todos = args.get("todos", [])
        if not isinstance(todos, list):
            return ToolResult("todos must be a list", status="error")
        self.agent.session["todos"] = todos
        return ToolResult("updated todos:\n" + json.dumps(todos, ensure_ascii=False, indent=2))

    def create_task(self, args: dict) -> ToolResult:
        task = self.agent.tasks.create(
            str(args["subject"]),
            str(args.get("description", "")),
            list(args.get("blocked_by") or []),
        )
        return ToolResult(f"created {task.id}: {task.subject}")

    def list_tasks(self, args: dict) -> ToolResult:
        return ToolResult(self.agent.tasks.format())

    def claim_task(self, args: dict) -> ToolResult:
        return ToolResult(self.agent.tasks.claim(str(args["task_id"]), str(args.get("owner", "agent"))))

    def complete_task(self, args: dict) -> ToolResult:
        return ToolResult(self.agent.tasks.complete(str(args["task_id"])))

    def schedule_cron(self, args: dict) -> ToolResult:
        result = self.agent.cron.schedule(
            str(args["cron"]),
            str(args["prompt"]),
            bool(args.get("recurring", True)),
            bool(args.get("durable", True)),
        )
        if isinstance(result, str):
            return ToolResult(result, status="error")
        return ToolResult(f"scheduled {result.id}: {result.cron} -> {result.prompt}")

    def list_crons(self, args: dict) -> ToolResult:
        return ToolResult(self.agent.cron.list_text())

    def cancel_cron(self, args: dict) -> ToolResult:
        return ToolResult(self.agent.cron.cancel(str(args["job_id"])))


def build_registry(agent) -> dict[str, Tool]:
    kit = ToolKit(agent)
    return {
        "list_files": Tool("list_files", "List workspace files", {"limit": "optional int"}, kit.list_files),
        "read_file": Tool("read_file", "Read a workspace file", {"path": "str", "limit": "optional int"}, kit.read_file),
        "write_file": Tool("write_file", "Write a workspace file", {"path": "str", "content": "str"}, kit.write_file, True),
        "patch_file": Tool("patch_file", "Replace old_text once in a file", {"path": "str", "old_text": "str", "new_text": "str"}, kit.patch_file, True),
        "search": Tool("search", "Regex search workspace files", {"pattern": "regex str", "limit": "optional int"}, kit.search),
        "run_shell": Tool("run_shell", "Run a shell command in workspace", {"command": "str", "timeout": "optional int", "background": "optional bool"}, kit.run_shell, True),
        "todo_write": Tool("todo_write", "Update current run todo list", {"todos": "list of {content,status}"}, kit.todo_write),
        "create_task": Tool("create_task", "Create persistent project task", {"subject": "str", "description": "optional str", "blocked_by": "optional list"}, kit.create_task),
        "list_tasks": Tool("list_tasks", "List persistent project tasks", {}, kit.list_tasks),
        "claim_task": Tool("claim_task", "Claim a ready project task", {"task_id": "str", "owner": "optional str"}, kit.claim_task),
        "complete_task": Tool("complete_task", "Complete an in-progress project task", {"task_id": "str"}, kit.complete_task),
        "schedule_cron": Tool("schedule_cron", "Schedule a future agent prompt with 5-field cron", {"cron": "str", "prompt": "str", "recurring": "bool", "durable": "bool"}, kit.schedule_cron),
        "list_crons": Tool("list_crons", "List scheduled prompts", {}, kit.list_crons),
        "cancel_cron": Tool("cancel_cron", "Cancel scheduled prompt", {"job_id": "str"}, kit.cancel_cron),
    }

