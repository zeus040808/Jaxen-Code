"""磁盘上的 Agent Skills 存储。

每个 Skill 是 ``<root>/<name>/SKILL.md``。Prompt 只得到紧凑索引；只有模型
调用 skill_view 时，正文才进入下一轮上下文，这就是 progressive disclosure。
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(api[_ -]?key|access[_ -]?token|secret|password)\b\s*[:=]\s*\S+"),
)
INJECTION_PATTERN = re.compile(
    r"(?i)\b(ignore|disregard|override)\b.{0,40}\b(system|developer)\b.{0,20}\b(instruction|prompt|rule)s?\b"
)
MAX_SKILL_CHARS = 16_000
MAX_INDEX_CHARS = 8_000


class SkillError(ValueError):
    pass


class SkillValidationError(SkillError):
    pass


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: str
    auto_generated: bool = False
    source_signature: str = ""


def _frontmatter_value(value):
    value = str(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.replace("\\n", " ").strip()


def _parse_frontmatter(text):
    lines = str(text).splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillValidationError("SKILL.md must start with YAML frontmatter")
    values = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return values
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = _frontmatter_value(value)
    raise SkillValidationError("SKILL.md frontmatter is not closed")


class SkillStore:
    def __init__(self, root):
        self.root = Path(root)

    def _path(self, name):
        name = self.validate_name(name)
        path = (self.root / name / "SKILL.md").resolve()
        root = self.root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise SkillValidationError("skill path escapes the skill store") from exc
        return path

    @staticmethod
    def validate_name(name):
        name = str(name).strip().lower()
        if not NAME_PATTERN.fullmatch(name):
            raise SkillValidationError("skill name must use lowercase letters, digits, and hyphens")
        return name

    @staticmethod
    def validate_text(text):
        text = str(text)
        if not text.strip():
            raise SkillValidationError("skill content must not be empty")
        if len(text) > MAX_SKILL_CHARS:
            raise SkillValidationError(f"skill content exceeds {MAX_SKILL_CHARS} characters")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise SkillValidationError("skill content looks like a secret")
        if INJECTION_PATTERN.search(text):
            raise SkillValidationError("skill content tries to override higher-priority instructions")
        return text.rstrip() + "\n"

    def list(self):
        if not self.root.exists():
            return []
        result = []
        for directory in sorted(self.root.iterdir(), key=lambda path: path.name):
            if not directory.is_dir() or directory.is_symlink():
                continue
            path = directory / "SKILL.md"
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
                meta = _parse_frontmatter(text)
                name = self.validate_name(meta.get("name", directory.name))
                if name != directory.name:
                    continue
                description = str(meta.get("description", "")).strip()
                if not description:
                    continue
                result.append(
                    SkillInfo(
                        name=name,
                        description=description,
                        path=path.as_posix(),
                        auto_generated=str(meta.get("auto_generated", "false")).lower() == "true",
                        source_signature=str(meta.get("source_signature", "")),
                    )
                )
            except (OSError, UnicodeError, SkillError):
                continue
        return result

    def render_index(self):
        lines = [
            "Available skills (procedures; bodies load only with skill_view):",
            "- Match the current task to a description. Call skill_view before following a matching skill.",
        ]
        skills = self.list()
        if not skills:
            lines.append("- none")
        else:
            for skill in skills:
                lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)[:MAX_INDEX_CHARS]

    def read(self, name):
        path = self._path(name)
        if not path.is_file():
            raise SkillError(f"skill not found: {name}")
        text = self.validate_text(path.read_text(encoding="utf-8"))
        meta = _parse_frontmatter(text)
        if self.validate_name(meta.get("name", "")) != self.validate_name(name):
            raise SkillValidationError("skill frontmatter name does not match its directory")
        return text

    def has_signature(self, signature):
        signature = str(signature).strip()
        return bool(signature) and any(skill.source_signature == signature for skill in self.list())

    def create(self, name, description, body, *, auto_generated=False, source_signature=""):
        name = self.validate_name(name)
        description = " ".join(str(description).split()).strip()
        if not description or len(description) > 240:
            raise SkillValidationError("skill description must contain 1 to 240 characters")
        path = self._path(name)
        if path.exists():
            raise SkillError(f"skill already exists: {name}")
        quoted_description = json.dumps(description, ensure_ascii=False)
        text = "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: {quoted_description}",
                f"auto_generated: {'true' if auto_generated else 'false'}",
                f"source_signature: {str(source_signature).strip()}",
                "---",
                "",
                str(body).strip(),
                "",
            ]
        )
        return self._atomic_write(path, self.validate_text(text))

    def patch(self, name, old_text, new_text):
        path = self._path(name)
        current = self.read(name)
        old_text = str(old_text)
        if not old_text or current.count(old_text) != 1:
            raise SkillValidationError("old_text must match exactly once")
        updated = self.validate_text(current.replace(old_text, str(new_text), 1))
        meta = _parse_frontmatter(updated)
        if self.validate_name(meta.get("name", "")) != self.validate_name(name):
            raise SkillValidationError("a patch cannot rename a skill")
        return self._atomic_write(path, updated)

    def delete(self, name):
        path = self._path(name)
        if not path.is_file():
            raise SkillError(f"skill not found: {name}")
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return path

    @staticmethod
    def signature(value):
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _atomic_write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=path.parent, prefix="SKILL.", suffix=".tmp"
            ) as handle:
                handle.write(text)
                handle.flush()
                temp_name = handle.name
            Path(temp_name).replace(path)
        finally:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
        return path
