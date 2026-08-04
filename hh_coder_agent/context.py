"""Prompt construction and context budgeting."""

from __future__ import annotations

import json

from .workspace import clip, sha256_text


class ContextManager:
    def __init__(self, agent):
        self.agent = agent

    def tool_text(self) -> str:
        return "\n".join(tool.signature() for tool in self.agent.tools.values())

    def history_text(self) -> str:
        rows = []
        history = self.agent.session.get("history", [])
        recent_start = max(0, len(history) - 8)
        for idx, item in enumerate(history):
            limit = 1200 if idx >= recent_start else 260
            if item.get("role") == "tool":
                rows.append(f"[tool:{item.get('name')}] {json.dumps(item.get('args', {}), ensure_ascii=False)}")
                rows.append(clip(item.get("content", ""), limit))
            else:
                rows.append(f"[{item.get('role')}] {clip(item.get('content', ''), limit)}")
        return clip("\n".join(rows) or "- empty", 40_000)

    def memory_text(self) -> str:
        memory = self.agent.session.get("memory", {})
        notes = memory.get("notes", [])[-12:]
        files = memory.get("file_summaries", {})
        return clip(
            "Notes:\n" + "\n".join(f"- {note}" for note in notes)
            + "\n\nFile summaries:\n"
            + "\n".join(f"- {path}: {summary}" for path, summary in files.items()),
            12_000,
        )

    def build(self, user_message: str) -> tuple[str, dict]:
        workspace_text = self.agent.workspace.summary()
        prompt = f"""You are HH Coder Agent, a local coding agent working inside one repository.

Rules:
- Think and answer in Chinese unless the user asks otherwise.
- Prefer small, reviewable edits.
- Use tools when repository facts are needed.
- Never invent file contents you have not read.
- Before multi-step work, call todo_write.
- Use persistent tasks for long-running project plans.
- For slow shell commands, set background=true.
- Reply with exactly one of these forms:
  <tool>{{"name":"read_file","args":{{"path":"README.md"}}}}</tool>
  <tool name="write_file" path="notes.txt"><content>hello</content></tool>
  <final>your final answer</final>

Tools:
{self.tool_text()}

Workspace:
{workspace_text}

Memory:
{self.memory_text()}

History:
{self.history_text()}

Current user request:
{user_message}
"""
        metadata = {
            "prompt_chars": len(prompt),
            "history_chars": len(self.history_text()),
            "workspace_chars": len(workspace_text),
            "memory_chars": len(self.memory_text()),
            "prompt_hash": sha256_text(prompt[:80_000]),
        }
        return clip(prompt, 120_000), metadata

