"""Current-task source evidence kept independently from the transcript.

Session history is the audit log.  This module keeps the much smaller set of
facts that the next model decision must not forget when raw tool output is
compressed away.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from .workspace import clip, file_freshness, now


EVIDENCE_SCHEMA_VERSION = "v1"
FILE_EVIDENCE_LIMIT = 24
ACTION_LIMIT = 24


def default_source_evidence_state():
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "goal_id": "",
        "next_id": 1,
        "files": {},
        "actions": [],
        "updated_at": now(),
    }


def _names(nodes, limit=14):
    return [str(node.name) for node in nodes[:limit] if getattr(node, "name", "")]


def summarize_source_text(path, text, limit=700):
    """Build a deterministic code-oriented summary without another LLM call."""

    path = str(path)
    text = str(text)
    lines = text.splitlines()
    parts = []
    suffix = Path(path).suffix.lower()

    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            doc = ast.get_docstring(tree, clean=True) or ""
            if doc:
                parts.append("purpose=" + clip(" ".join(doc.split()), 180))
            classes = _names([node for node in tree.body if isinstance(node, ast.ClassDef)])
            functions = _names(
                [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            )
            imports = []
            for node in tree.body:
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = ("." * int(node.level)) + str(node.module or "")
                    imports.append(module)
            if classes:
                parts.append("classes=" + ", ".join(classes))
            if functions:
                parts.append("functions=" + ", ".join(functions))
            if imports:
                parts.append("imports=" + ", ".join(dict.fromkeys(imports[:12])))

    if not parts:
        class_names = re.findall(r"^\s*class\s+([A-Za-z_]\w*)", text, re.M)
        function_names = re.findall(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", text, re.M)
        headings = re.findall(r"^#{1,3}\s+(.+)$", text, re.M)
        if class_names:
            parts.append("classes=" + ", ".join(dict.fromkeys(class_names[:14])))
        if function_names:
            parts.append("functions=" + ", ".join(dict.fromkeys(function_names[:18])))
        if headings:
            parts.append("headings=" + ", ".join(dict.fromkeys(headings[:12])))

    if not parts:
        meaningful = [line.strip() for line in lines if line.strip()]
        parts.append("content=" + (" | ".join(meaningful[:4]) if meaningful else "(empty)"))

    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return clip("; ".join(parts) + f"; excerpt_sha256={digest}", limit)


class SourceEvidence:
    """Small, freshness-aware evidence ledger for one logical user task."""

    def __init__(self, state=None, workspace_root=None):
        self.root = Path(workspace_root).resolve() if workspace_root is not None else None
        self.state = state if isinstance(state, dict) else default_source_evidence_state()
        self._ensure_shape()

    def _ensure_shape(self):
        defaults = default_source_evidence_state()
        for key, value in defaults.items():
            if key not in self.state:
                self.state[key] = dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value
        if not isinstance(self.state.get("files"), dict):
            self.state["files"] = {}
        if not isinstance(self.state.get("actions"), list):
            self.state["actions"] = []
        self.state["schema_version"] = EVIDENCE_SCHEMA_VERSION

    def to_dict(self):
        self._ensure_shape()
        return self.state

    def begin(self, goal_id):
        goal_id = str(goal_id or "").strip()
        if goal_id and goal_id == str(self.state.get("goal_id", "")):
            return
        self.state.clear()
        self.state.update(default_source_evidence_state())
        self.state["goal_id"] = goal_id

    def _canonical(self, raw_path):
        path = Path(str(raw_path))
        if self.root is None:
            return path.as_posix()
        resolved = path if path.is_absolute() else self.root / path
        resolved = resolved.resolve()
        return resolved.relative_to(self.root).as_posix()

    def _freshness(self, path):
        if self.root is None:
            return None
        return file_freshness(path, self.root)

    def invalidate_stale_files(self):
        """Drop file evidence whose content no longer matches the workspace."""

        stale_paths = []
        for path, item in list(self.state["files"].items()):
            if item.get("freshness") == self._freshness(path):
                continue
            stale_paths.append(path)
            self.state["files"].pop(path, None)
        if stale_paths:
            self.state["updated_at"] = now()
        return stale_paths

    def record_file(self, path, summary, start=1, end=None, total_lines=None, source_tool="read_file"):
        canonical = self._canonical(path)
        existing = self.state["files"].get(canonical, {})
        evidence_id = existing.get("id")
        if not evidence_id:
            evidence_id = f"E{int(self.state.get('next_id', 1))}"
            self.state["next_id"] = int(self.state.get("next_id", 1)) + 1
        item = {
            "id": evidence_id,
            "path": canonical,
            "summary": clip(str(summary).strip(), 700),
            "start": int(start or 1),
            "end": int(end) if end is not None else None,
            "total_lines": int(total_lines) if total_lines is not None else None,
            "complete": bool(total_lines is not None and int(start or 1) == 1 and end is not None and int(end) >= int(total_lines)),
            "freshness": self._freshness(canonical),
            "source_tool": str(source_tool),
            "updated_at": now(),
        }
        self.state["files"][canonical] = item
        if len(self.state["files"]) > FILE_EVIDENCE_LIMIT:
            oldest = sorted(self.state["files"].values(), key=lambda value: value.get("updated_at", ""))
            for stale in oldest[: len(self.state["files"]) - FILE_EVIDENCE_LIMIT]:
                self.state["files"].pop(stale["path"], None)
        self.state["updated_at"] = now()
        return item

    def record_action(self, name, args, status="ok", cache_hit=False):
        action = {
            "tool": str(name),
            "args": args or {},
            "status": str(status),
            "cache_hit": bool(cache_hit),
            "created_at": now(),
        }
        self.state["actions"].append(action)
        self.state["actions"] = self.state["actions"][-ACTION_LIMIT:]
        self.state["updated_at"] = now()

    def file_summary(self, path):
        try:
            canonical = self._canonical(path)
        except (OSError, ValueError):
            return ""
        item = self.state["files"].get(canonical, {})
        if not item or item.get("freshness") != self._freshness(canonical):
            return ""
        return str(item.get("summary", "")).strip()

    def render(self, budget=None):
        lines = [
            "Verified source evidence:",
            "- purpose: current-task code evidence that survives transcript compression",
        ]
        valid = []
        for item in self.state["files"].values():
            if item.get("freshness") != self._freshness(item.get("path", "")):
                continue
            valid.append(item)
        valid.sort(key=lambda item: int(str(item.get("id", "E0"))[1:] or 0))
        if not valid:
            lines.append("- files: none")
        else:
            lines.append("- files:")
            for item in valid:
                range_text = f"lines {item['start']}-{item['end'] or '?'}"
                coverage = "complete" if item.get("complete") else "partial"
                lines.append(
                    f"  - {item['id']} {item['path']} ({range_text}, {coverage}): {item['summary']}"
                )

        successful = [
            action for action in self.state["actions"]
            if action.get("status") in {"ok", "cached"}
        ][-10:]
        if successful:
            lines.append("- already_observed_calls (do not repeat unchanged):")
            for action in successful:
                cached = " [cache-hit]" if action.get("cache_hit") else ""
                lines.append(f"  - {action['tool']} {clip(str(action['args']), 280)}{cached}")
        else:
            lines.append("- already_observed_calls: none")
        rendered = "\n".join(lines)
        if budget is None or len(rendered) <= int(budget):
            return rendered

        budget = max(80, int(budget))
        header = [
            "Verified source evidence:",
            "- purpose: current-task code evidence that survives transcript compression",
            "- files:",
        ]
        overhead = len("\n".join(header)) + 1
        per_file = max(60, (budget - overhead) // max(1, len(valid)))
        compact = list(header)
        for item in valid:
            prefix = f"  - {item['id']} {item['path']}: "
            summary_budget = max(12, per_file - len(prefix) - 1)
            compact.append(prefix + clip(item["summary"], summary_budget))
        rendered = "\n".join(compact)
        if len(rendered) <= budget:
            return rendered

        # Extremely small custom budgets keep the newest evidence entries.
        compact = list(header)
        for item in reversed(valid):
            line = f"  - {item['id']} {item['path']}: {clip(item['summary'], 120)}"
            candidate = "\n".join([*compact, line])
            if len(candidate) > budget:
                break
            compact.append(line)
        return clip("\n".join(compact), budget)
