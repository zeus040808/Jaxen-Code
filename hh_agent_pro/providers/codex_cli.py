"""Use the installed Codex CLI as HH Agent Pro's decision model.

Codex runs ephemerally and read-only. It may reason and return an HH tool request,
but only HH's ToolExecutor is allowed to mutate the workspace.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path


CODEX_ENV_ALLOWLIST = {
    "APPDATA",
    "CODEX_HOME",
    "COMSPEC",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LOCALAPPDATA",
    "NO_PROXY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "WINDIR",
}

MODEL_BOUNDARY = """\
You are the decision model inside HH Agent Pro, not the workspace executor.
Do not use Codex CLI tools, shell commands, file edits, web search, MCP tools, or subagents.
Do not independently inspect the filesystem. The outer HH runtime supplies all available context
and executes only the explicit HH tool request you return. Follow the response contract in the
prompt exactly. Think privately; expose only concise provider-supported reasoning summaries,
never private chain-of-thought. If information or a user decision is missing, return the required
<needs_user_input> question and include short numbered choices when the options are bounded.

HH prompt follows:
"""


def _text_value(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("summary")
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()
    return ""


class CodexCliModelClient:
    """Adapter around ``codex exec --json`` JSONL events."""

    supports_prompt_cache = False
    supports_native_tools = False

    def __init__(self, workspace_root, executable="codex", model=None, timeout=300):
        resolved = shutil.which(str(executable))
        if resolved is None and Path(str(executable)).is_file():
            resolved = str(Path(str(executable)).resolve())
        if not resolved:
            raise RuntimeError(
                "Codex CLI was not found. Install it and ensure `codex --version` works, "
                "or pass --codex-path."
            )
        self.executable = resolved
        self.workspace_root = str(Path(workspace_root).resolve())
        self.model = str(model).strip() if model else "codex-config-default"
        self.requested_model = str(model).strip() if model else ""
        self.timeout = max(10, int(timeout))
        self.last_completion_metadata = {}

    def _command(self):
        command = [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-C",
            self.workspace_root,
        ]
        if self.requested_model:
            command.extend(["--model", self.requested_model])
        # Read the prompt from stdin. Closing stdin after the write is important:
        # otherwise Codex may wait for "additional input" after turn.completed.
        command.append("-")
        return command

    @staticmethod
    def _environment():
        environment = {
            name: value
            for name, value in os.environ.items()
            if name.upper() in CODEX_ENV_ALLOWLIST
        }
        environment["NO_COLOR"] = "1"
        return environment

    @staticmethod
    def _emit(callback, event_type, **payload):
        if callback is not None:
            callback({"type": event_type, **payload})

    def _consume_event(self, event, state, callback=None):
        event_type = str(event.get("type", ""))
        if event_type == "thread.started":
            state["thread_id"] = str(event.get("thread_id", ""))
            self._emit(callback, "provider_status", message="Codex turn started")
            return
        if event_type == "turn.started":
            return
        if event_type == "error":
            message = str(event.get("message", "")).strip()
            if message:
                state["errors"].append(message)
                self._emit(callback, "provider_status", message=message)
            return
        if event_type == "turn.completed":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            state["usage"] = dict(usage)
            self._emit(callback, "provider_status", message="Codex turn completed")
            return
        if event_type not in {"item.started", "item.updated", "item.completed"}:
            return

        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = str(item.get("type", ""))
        if item_type == "reasoning" and event_type != "item.started":
            text = _text_value(item.get("text") or item.get("summary") or item.get("content"))
            if text and text not in state["reasoning_seen"]:
                state["reasoning_seen"].add(text)
                self._emit(callback, "reasoning_summary", text=text)
            return
        if item_type == "agent_message" and event_type == "item.completed":
            text = _text_value(item.get("text") or item.get("content"))
            if text:
                state["answer"] = text
            return
        if item_type in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
            # This should be prevented by MODEL_BOUNDARY and read-only sandbox. Surface it
            # visibly if a future Codex version still attempts an internal action.
            detail = _text_value(item.get("command") or item.get("text") or item.get("name"))
            self._emit(
                callback,
                "provider_status",
                message=f"Codex attempted internal {item_type}{': ' + detail if detail else ''}",
            )

    @staticmethod
    def _reader(stream, target_queue, stream_name):
        try:
            for line in iter(stream.readline, ""):
                target_queue.put((stream_name, line))
        finally:
            target_queue.put((stream_name, None))

    def complete(self, prompt, max_new_tokens, **kwargs):
        return self.complete_stream(prompt, max_new_tokens, **kwargs)

    def complete_stream(
        self,
        prompt,
        max_new_tokens,
        prompt_cache_key=None,
        prompt_cache_retention=None,
        event_callback=None,
        tools=None,
    ):
        del prompt_cache_key, prompt_cache_retention, tools
        state = {
            "answer": "",
            "errors": [],
            "reasoning_seen": set(),
            "thread_id": "",
            "usage": {},
        }
        command = self._command()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=self.workspace_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._environment(),
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RuntimeError(f"Could not start Codex CLI: {exc}") from exc

        try:
            process.stdin.write(MODEL_BOUNDARY + str(prompt))
            process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            process.kill()
            raise RuntimeError(f"Codex CLI closed before receiving the prompt: {exc}") from exc

        output_queue = queue.Queue()
        stdout_thread = threading.Thread(
            target=self._reader, args=(process.stdout, output_queue, "stdout"), daemon=True
        )
        stderr_thread = threading.Thread(
            target=self._reader, args=(process.stderr, output_queue, "stderr"), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        stderr_lines = []
        completed_streams = set()
        deadline = time.monotonic() + self.timeout
        try:
            while len(completed_streams) < 2:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    raise RuntimeError(f"Codex CLI timed out after {self.timeout} seconds")
                try:
                    stream_name, line = output_queue.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    completed_streams.add(stream_name)
                    continue
                if stream_name == "stderr":
                    stripped = line.strip()
                    if stripped:
                        stderr_lines.append(stripped)
                        del stderr_lines[:-30]
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self._consume_event(event, state, callback=event_callback)
        except BaseException:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise

        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=5)

        usage = state["usage"]
        self.last_completion_metadata = {
            "provider": "codex-cli",
            "model": self.model,
            "codex_thread_id": state["thread_id"],
            "input_tokens": usage.get("input_tokens"),
            "cached_tokens": usage.get("cached_input_tokens", 0),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
            "max_new_tokens_requested": int(max_new_tokens),
        }
        if state["answer"]:
            return state["answer"]
        details = state["errors"][-3:] or stderr_lines[-5:]
        suffix = "\n" + "\n".join(details) if details else ""
        if return_code:
            raise RuntimeError(
                f"Codex CLI exited with code {return_code}. Run `codex login status` and "
                f"`codex --version` to verify the installation.{suffix}"
            )
        raise RuntimeError(f"Codex CLI completed without an agent message.{suffix}")
