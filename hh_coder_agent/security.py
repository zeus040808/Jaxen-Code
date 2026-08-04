"""Security policy for tool execution."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


REDACTED = "[REDACTED]"
DANGEROUS_SHELL = (
    "rm -rf /", "sudo ", "shutdown", "reboot", "format ", "del /s",
    "Remove-Item -Recurse", "chmod 777", "> /etc/",
)
SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|token|secret|password|sk-[A-Za-z0-9_-]{8,})")


def redact_text(text: object) -> str:
    return SECRET_PATTERN.sub(REDACTED, str(text))


def redacted_json(value: object) -> object:
    if isinstance(value, dict):
        return {k: redacted_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted_json(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def shell_env(root: Path) -> dict:
    allow = ("PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG", "PYTHONUTF8")
    env = {name: os.environ[name] for name in allow if name in os.environ}
    env["PWD"] = str(root)
    return env


class ApprovalPolicy:
    def __init__(self, mode: str = "ask", read_only: bool = False):
        if mode not in {"ask", "auto", "never"}:
            raise ValueError("approval policy must be ask, auto, or never")
        self.mode = mode
        self.read_only = read_only

    def check_shell(self, command: str) -> str | None:
        lowered = command.lower()
        for token in DANGEROUS_SHELL:
            if token.lower() in lowered:
                return f"dangerous shell pattern blocked: {token}"
        return None

    def needs_approval(self, tool_name: str, args: dict) -> str | None:
        if self.read_only and tool_name in {"write_file", "patch_file", "run_shell"}:
            return "read-only agent cannot mutate workspace"
        if tool_name == "run_shell":
            return self.check_shell(str(args.get("command", "")))
        if tool_name in {"write_file", "patch_file"} and self.mode == "ask":
            return f"{tool_name} may modify files"
        return None

    def approve(self, tool_name: str, args: dict) -> tuple[bool, str]:
        reason = self.needs_approval(tool_name, args)
        if not reason:
            return True, "allowed"
        if self.mode == "auto" and not self.read_only:
            return True, reason
        if self.mode == "never" or self.read_only:
            return False, reason
        answer = input(f"approve {tool_name} {json.dumps(args, ensure_ascii=False)}? {reason} [y/N] ")
        return answer.strip().lower() in {"y", "yes"}, reason

