"""把已经验证成功的复杂工具流程确定性地沉淀为 Skill。"""

from __future__ import annotations

import re
from pathlib import Path

from ..workspace import clip
from .store import SkillError, SkillStore


MUTATION_TOOLS = {"write_file", "patch_file"}
IGNORED_ARGS = {"content", "old_text", "new_text", "operations"}


def _slug(value):
    value = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return value[:32]


class SkillLearner:
    """Conservative auto-learning: successful edit + later validation + 5 tools."""

    def __init__(self, store: SkillStore, minimum_tool_steps=5):
        self.store = store
        self.minimum_tool_steps = int(minimum_tool_steps)

    @staticmethod
    def _safe_args(action):
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        safe = []
        for key in sorted(args):
            if key in IGNORED_ARGS:
                continue
            value = args[key]
            if key == "command":
                value = clip(" ".join(str(value).split()), 240)
            elif isinstance(value, (dict, list)):
                value = clip(str(value), 180)
            else:
                value = clip(str(value), 180)
            safe.append(f"{key}={value}")
        return ", ".join(safe)

    def consider(self, user_request, final_answer, run_state, evidence_state):
        if str(getattr(run_state, "status", "")) != "completed":
            return {"status": "skipped", "reason": "run_not_completed"}
        if int(getattr(run_state, "tool_steps", 0)) < self.minimum_tool_steps:
            return {"status": "skipped", "reason": "too_few_tool_steps"}

        actions = list((evidence_state or {}).get("actions", []))
        actions = [action for action in actions if not str(action.get("tool", "")).startswith("skill_")]
        successful = [action for action in actions if action.get("status") in {"ok", "cached"}]
        mutation_indexes = [
            index for index, action in enumerate(actions)
            if action.get("tool") in MUTATION_TOOLS and action.get("status") == "ok"
        ]
        verified_after_edit = any(
            index > min(mutation_indexes)
            and action.get("tool") == "run_shell"
            and action.get("status") == "ok"
            for index, action in enumerate(actions)
        ) if mutation_indexes else False
        if len(successful) < self.minimum_tool_steps or not verified_after_edit:
            return {"status": "skipped", "reason": "no_verified_reusable_edit_workflow"}

        normalized = [
            {"tool": action.get("tool", ""), "args": self._safe_args(action)}
            for action in successful
        ]
        signature = self.store.signature(normalized)
        if self.store.has_signature(signature):
            return {"status": "duplicate", "signature": signature}

        first_path = next(
            (
                str((action.get("args") or {}).get("path", ""))
                for action in successful
                if str((action.get("args") or {}).get("path", "")).strip()
            ),
            "",
        )
        stem = _slug(Path(first_path).stem) if first_path else "workflow"
        name = f"learned-{stem or 'workflow'}-{signature[:8]}"
        description = clip("Auto-learned verified workflow for: " + " ".join(str(user_request).split()), 220)

        procedure = []
        for index, action in enumerate(successful, 1):
            detail = self._safe_args(action)
            procedure.append(f"{index}. Run `{action.get('tool', '')}`" + (f" with `{detail}`." if detail else "."))
        validation = [
            self._safe_args(action)
            for action in successful
            if action.get("tool") == "run_shell" and self._safe_args(action)
        ]
        body = "\n".join(
            [
                f"# {name}",
                "",
                "## When to use",
                description,
                "",
                "## Preconditions",
                "- Work inside the same repository type and inspect current files before editing.",
                "- Treat paths and commands below as a recipe, not immutable facts.",
                "",
                "## Procedure",
                *procedure,
                "",
                "## Verification",
                *([f"- Re-run `{item}` and require a successful exit." for item in validation] or ["- Re-run the repository's relevant checks."]),
                "",
                "## Origin",
                f"- Learned from a completed run with {len(successful)} successful tool actions.",
                f"- Final result summary: {clip(str(final_answer), 300)}",
            ]
        )
        try:
            path = self.store.create(
                name,
                description,
                body,
                auto_generated=True,
                source_signature=signature,
            )
        except SkillError as exc:
            return {"status": "error", "reason": str(exc)}
        return {"status": "created", "name": name, "path": str(path), "signature": signature}
