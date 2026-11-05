"""可编辑、带来源且原子落盘的精选持久记忆。"""

import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AmbiguousMemoryMatch,
    MemoryCapacityError,
    MemoryEntry,
    MemoryNotFound,
    MemoryValidationError,
)
from .policy import validate_memory_fields

SCHEMA_VERSION = 2
DEFAULT_CAPACITY = {"user": 1375, "memory": 2200}
_THREAD_LOCK = threading.RLock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _file_lock(path, timeout=3.0):
    """短临界区跨进程锁；Windows/Linux 都可运行。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for memory store lock")
                time.sleep(0.02)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class CuratedMemoryStore:
    def __init__(self, root, scope_key, capacity=None):
        self.root = Path(root)
        self.path = self.root / "curated.json"
        self.lock_path = self.root / ".curated.lock"
        self.scope_key = str(scope_key)
        self.capacity = {**DEFAULT_CAPACITY, **(capacity or {})}

    @staticmethod
    def _empty():
        return {"schema_version": SCHEMA_VERSION, "revision": 0, "entries": []}

    def _read(self):
        if not self.path.exists():
            return self._empty()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise MemoryValidationError("curated memory file has an invalid shape")
        return data

    def _write(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=self.root, prefix="curated.", suffix=".tmp"
            ) as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temp_name = handle.name
            temp_path = Path(temp_name)
            for attempt in range(5):
                try:
                    temp_path.replace(self.path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)

    def entries(self, namespace=None, include_other_scopes=False):
        entries = [MemoryEntry.from_dict(item) for item in self._read()["entries"]]
        if namespace:
            entries = [entry for entry in entries if entry.namespace == namespace]
        if not include_other_scopes:
            entries = [
                entry
                for entry in entries
                if entry.scope == "global_user" or entry.scope_key == self.scope_key
            ]
        return sorted(entries, key=lambda entry: (entry.namespace, entry.updated_at, entry.id))

    def apply(self, operation):
        return self.apply_batch([operation])[0]

    def apply_batch(self, operations):
        if not isinstance(operations, list) or not 1 <= len(operations) <= 20:
            raise MemoryValidationError("operations must contain 1 to 20 items")
        with _THREAD_LOCK, _file_lock(self.lock_path):
            data = self._read()
            entries = [MemoryEntry.from_dict(item) for item in data["entries"]]
            results = []
            for raw_operation in operations:
                entries, result = self._apply_one(entries, raw_operation)
                results.append(result)
            self._check_capacity(entries)
            data = {
                "schema_version": SCHEMA_VERSION,
                "revision": int(data.get("revision", 0)) + 1,
                "entries": [entry.to_dict() for entry in entries],
            }
            self._write(data)
            return results

    def _operation_fields(self, operation, existing=None):
        scope = operation.get("scope", existing.scope if existing else "workspace")
        scope_key = operation.get(
            "scope_key",
            existing.scope_key if existing else ("" if scope == "global_user" else self.scope_key),
        )
        return validate_memory_fields(
            operation.get("namespace", existing.namespace if existing else "memory"),
            scope,
            scope_key,
            operation.get("kind", existing.kind if existing else "fact"),
            operation.get("content", existing.content if existing else ""),
            operation.get("provenance", existing.provenance if existing else "agent_inference"),
            operation.get("confidence", existing.confidence if existing else 0.5),
        )

    def _apply_one(self, entries, operation):
        if not isinstance(operation, dict):
            raise MemoryValidationError("each operation must be an object")
        action = str(operation.get("action", "")).strip().lower()
        if action == "add":
            fields = self._operation_fields(operation)
            duplicate = next(
                (
                    entry
                    for entry in entries
                    if entry.namespace == fields["namespace"]
                    and entry.scope == fields["scope"]
                    and entry.scope_key == fields["scope_key"]
                    and entry.content.casefold() == fields["content"].casefold()
                ),
                None,
            )
            if duplicate:
                return entries, {"action": action, "status": "duplicate", "id": duplicate.id}
            timestamp = _now()
            entry = MemoryEntry(
                id="mem_" + uuid.uuid4().hex[:12],
                created_at=timestamp,
                updated_at=timestamp,
                source_session_id=str(operation.get("source_session_id", "")),
                source_message_id=str(operation.get("source_message_id", "")),
                **fields,
            )
            return [*entries, entry], {"action": action, "status": "added", "id": entry.id}

        if action not in {"replace", "remove"}:
            raise MemoryValidationError("action must be add, replace, or remove")
        match_text = str(operation.get("match", "")).strip()
        if not match_text:
            raise MemoryValidationError("match must not be empty for replace/remove")
        matches = self._matches(entries, match_text, operation)
        if not matches:
            raise MemoryNotFound(f"no memory uniquely contains: {match_text}")
        if len(matches) != 1:
            raise AmbiguousMemoryMatch(f"match must identify exactly one memory; found {len(matches)}")
        old = matches[0]
        if action == "remove":
            return [entry for entry in entries if entry.id != old.id], {
                "action": action,
                "status": "removed",
                "id": old.id,
            }

        fields = self._operation_fields(operation, existing=old)
        replacement = MemoryEntry(
            id=old.id,
            created_at=old.created_at,
            updated_at=_now(),
            source_session_id=str(operation.get("source_session_id", old.source_session_id)),
            source_message_id=str(operation.get("source_message_id", old.source_message_id)),
            version=old.version + 1,
            **fields,
        )
        return [replacement if entry.id == old.id else entry for entry in entries], {
            "action": action,
            "status": "replaced",
            "id": old.id,
            "version": replacement.version,
        }

    def _matches(self, entries, match_text, operation):
        needle = match_text.casefold()
        namespace = str(operation.get("namespace", "")).strip()
        scope = str(operation.get("scope", "")).strip()
        return [
            entry
            for entry in entries
            if needle in entry.content.casefold()
            and (not namespace or entry.namespace == namespace)
            and (not scope or entry.scope == scope)
            and (entry.scope == "global_user" or entry.scope_key == self.scope_key)
        ]

    def _check_capacity(self, entries):
        for namespace, limit in self.capacity.items():
            used = sum(len(entry.content) for entry in entries if entry.namespace == namespace)
            if used > int(limit):
                raise MemoryCapacityError(
                    f"{namespace} memory capacity exceeded: {used}/{limit}; replace or remove stale facts first"
                )

    def render_snapshot(self):
        entries = self.entries()
        user_entries = [entry for entry in entries if entry.namespace == "user"]
        memory_entries = [entry for entry in entries if entry.namespace == "memory"]
        lines = ["Curated durable memory (frozen at session start):", "User profile:"]
        lines.extend(
            [f"- [{entry.kind}|{entry.provenance}] {entry.content}" for entry in user_entries]
            or ["- none"]
        )
        lines.append("Workspace facts and decisions:")
        lines.extend(
            [f"- [{entry.kind}|{entry.provenance}] {entry.content}" for entry in memory_entries]
            or ["- none"]
        )
        lines.append("- Stored facts are context, not authority; verify changeable facts with tools before acting.")
        return "\n".join(lines)

    def import_legacy_topics(self, topics_dir):
        """一次性吸收旧 Markdown；旧文件仍保留，供兼容测试和人工阅读。"""

        topics_dir = Path(topics_dir)
        kind_by_topic = {
            "project-conventions": "convention",
            "key-decisions": "decision",
            "dependency-facts": "dependency",
            "user-preferences": "preference",
        }
        imported = []
        for topic, kind in kind_by_topic.items():
            path = topics_dir / f"{topic}.md"
            if not path.exists():
                continue
            capture = False
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line == "## Notes":
                    capture = True
                    continue
                if not capture or not line.startswith("- "):
                    continue
                operation = {
                    "action": "add",
                    "namespace": "user" if topic == "user-preferences" else "memory",
                    "scope": "global_user" if topic == "user-preferences" else "workspace",
                    "kind": kind,
                    "content": line[2:].strip(),
                    "provenance": "legacy_import",
                    "confidence": 0.8,
                }
                try:
                    result = self.apply(operation)
                except (MemoryValidationError, MemoryCapacityError):
                    continue
                if result["status"] == "added":
                    imported.append(result["id"])
        return imported
