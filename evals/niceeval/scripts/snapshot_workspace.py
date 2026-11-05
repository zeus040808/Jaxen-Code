"""Create a deterministic before-snapshot for the hidden hard grader."""

import argparse
import hashlib
import json
from pathlib import Path


IGNORED_NAMES = {
    ".git",
    ".jaxen",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def snapshot(root):
    root = Path(root).resolve()
    files = {}
    fixture = hashlib.sha256()
    candidates = []
    for path in root.rglob("*"):
        # A security fixture may intentionally contain an escaping symlink.
        # Never follow or hash it as trusted fixture content.
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_NAMES for part in relative.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        candidates.append(path)
    for path in sorted(candidates, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        content_digest = hashlib.sha256(path.read_bytes()).digest()
        files[relative] = content_digest.hex()
        fixture.update(relative.encode("utf-8"))
        fixture.update(b"\0")
        fixture.update(content_digest)
    return {"files": files, "fixture_digest": "sha256:" + fixture.hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = snapshot(args.workspace)
    Path(args.output).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
