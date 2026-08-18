"""Claude-Code-style terminal rendering for HH observable events."""

from __future__ import annotations

import json
import sys

from .workspace import clip


class TerminalEventSink:
    def __init__(self, stream=None, use_color=None):
        self.stream = stream or sys.stdout
        self.use_color = self.stream.isatty() if use_color is None else bool(use_color)
        self.final_rendered = False
        self.needs_input = False

    def reset_turn(self):
        self.final_rendered = False
        self.needs_input = False

    def _paint(self, text, code):
        if not self.use_color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def _write(self, text=""):
        text = str(text)
        encoding = getattr(self.stream, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_text, file=self.stream, flush=True)

    @staticmethod
    def _one_line(value, limit=240):
        return clip(" ".join(str(value).split()), limit)

    def emit(self, event_type, **payload):
        if event_type == "run_started":
            self.reset_turn()
            return
        if event_type == "status":
            self._write(self._paint(f"* {payload.get('message', 'Working')}", "36"))
            return
        if event_type == "provider_status":
            message = self._one_line(payload.get("message", ""))
            if message:
                self._write(self._paint(f"  -> {message}", "90"))
            return
        if event_type == "reasoning_summary":
            text = str(payload.get("text", "")).strip()
            if text:
                self._write(self._paint("  Reasoning summary", "35;1"))
                for line in text.splitlines():
                    self._write(f"  {line}")
            return
        if event_type == "tool_started":
            name = str(payload.get("name", "tool"))
            args = json.dumps(payload.get("args", {}), ensure_ascii=False, sort_keys=True)
            self._write(self._paint(f"* {name}", "33;1") + f" {self._one_line(args, 500)}")
            return
        if event_type == "tool_finished":
            metadata = payload.get("metadata") or {}
            status = metadata.get("tool_status", "ok")
            marker = "[ok]" if status == "ok" else "[!]"
            color = "32" if status == "ok" else "31"
            self._write(self._paint(f"  {marker} {status}", color))
            result = str(payload.get("result", "")).strip()
            if result:
                for line in clip(result, 1200).splitlines()[:12]:
                    self._write(self._paint(f"    {line}", "90"))
            return
        if event_type == "needs_user_input":
            self.needs_input = True
            question = str(payload.get("question", "")).strip()
            self._write(self._paint("? Need your input", "33;1"))
            self._write(question)
            return
        if event_type == "approval_requested":
            name = str(payload.get("name", "tool"))
            args = json.dumps(payload.get("args", {}), ensure_ascii=False, sort_keys=True)
            self._write(self._paint(f"? Approval required - {name}", "33;1"))
            self._write(f"  {self._one_line(args, 600)}")
            return
        if event_type == "blocked":
            reason = str(payload.get("reason", "")).strip()
            self._write(self._paint("! Blocked", "31;1"))
            self._write(reason)
            return
        if event_type == "run_finished":
            final = str(payload.get("final_answer", "")).strip()
            if final and not self.needs_input:
                self._write(self._paint("* Answer", "32;1"))
                self._write(final)
            self.final_rendered = bool(final)
