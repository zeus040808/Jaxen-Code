"""Workspace discovery and small text helpers."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path


IGNORED_NAMES = {
    ".git", ".idea", ".venv", "__pycache__", ".pytest_cache",
    ".hh_coder", ".tmp", ".transcripts", "node_modules",
}
MAX_TEXT = 120_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clip(text: object, limit: int = 4000) -> str:
    value = str(text)
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[clipped {len(value) - limit} chars]"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def path(self, raw_path: str | Path) -> Path:
        path = Path(raw_path)
        resolved = (path if path.is_absolute() else self.root / path).resolve()
        if self.root != resolved and self.root not in resolved.parents:
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

    def rel(self, path: str | Path) -> str:
        return self.path(path).relative_to(self.root).as_posix()

    def iter_files(self, limit: int = 300) -> list[str]:
        files: list[str] = []
        for path in self.root.rglob("*"):
            try:
                rel_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_NAMES for part in rel_parts):
                continue
            if path.is_file():
                files.append(path.relative_to(self.root).as_posix())
            if len(files) >= limit:
                break
        return sorted(files)

    def project_docs(self) -> list[str]:
        names = ("AGENTS.md", "CLAUDE.md", "README.md", "pyproject.toml")
        docs = []
        for name in names:
            path = self.root / name
            if path.exists() and path.is_file():
                docs.append(f"## {name}\n{clip(path.read_text(encoding='utf-8', errors='replace'), 2500)}")
        return docs

    def fingerprint(self) -> str:
        sample = "\n".join(self.iter_files(limit=500))
        return sha256_text(str(self.root) + "\n" + sample)

    def summary(self) -> str:
        docs = "\n\n".join(self.project_docs()) or "(no project docs found)"
        files = "\n".join(f"- {path}" for path in self.iter_files(limit=120))
        return clip(
            f"Workspace root: {self.root}\n\nProject docs:\n{docs}\n\nFiles:\n{files}",
            MAX_TEXT,
        )

