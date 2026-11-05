"""持久记忆的准入、安全和中文检索规则。"""

import re

from .models import MemoryValidationError

VALID_NAMESPACES = {"memory", "user"}
VALID_SCOPES = {"global_user", "workspace", "project"}
VALID_KINDS = {"preference", "convention", "decision", "dependency", "fact", "correction"}
VALID_PROVENANCE = {
    "explicit_user",
    "user_correction",
    "verified_tool",
    "agent_inference",
    "legacy_import",
}

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(api[_ -]?key|access[_ -]?token|secret|password)\b\s*[:=]\s*\S+"),
)
INJECTION_PATTERNS = (
    re.compile(r"(?i)\b(ignore|disregard|override)\b.{0,40}\b(previous|system|developer)\b.{0,20}\b(instruction|prompt|rule)s?\b"),
    re.compile(r"(?i)\b(reveal|print|leak)\b.{0,30}\b(system prompt|hidden instruction)s?\b"),
    re.compile(r"(忽略|绕过|覆盖).{0,20}(系统|开发者|之前).{0,10}(指令|规则|提示词)"),
    re.compile(r"(泄露|输出|显示).{0,20}(系统提示词|隐藏指令)"),
)


def tokenize(text):
    """可解释的中英文 tokenizer。

    旧实现只识别 ASCII。这里对连续中文同时生成单字、双字和完整词串，
    因而“数据库迁移”可以召回“迁移数据库时使用 Alembic”。
    """

    tokens = {item.lower() for item in re.findall(r"[A-Za-z0-9_]+", str(text))}
    for run in re.findall(r"[\u3400-\u9fff]+", str(text)):
        tokens.add(run)
        tokens.update(run)
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return {token for token in tokens if token}


def validate_memory_fields(namespace, scope, scope_key, kind, content, provenance, confidence):
    namespace = str(namespace).strip()
    scope = str(scope).strip()
    scope_key = str(scope_key).strip()
    kind = str(kind).strip()
    content = str(content).strip()
    provenance = str(provenance).strip()

    if namespace not in VALID_NAMESPACES:
        raise MemoryValidationError(f"namespace must be one of {sorted(VALID_NAMESPACES)}")
    if scope not in VALID_SCOPES:
        raise MemoryValidationError(f"scope must be one of {sorted(VALID_SCOPES)}")
    if scope != "global_user" and not scope_key:
        raise MemoryValidationError("scope_key is required for workspace/project memory")
    if kind not in VALID_KINDS:
        raise MemoryValidationError(f"kind must be one of {sorted(VALID_KINDS)}")
    if provenance not in VALID_PROVENANCE:
        raise MemoryValidationError(f"provenance must be one of {sorted(VALID_PROVENANCE)}")
    if not content:
        raise MemoryValidationError("content must not be empty")
    if len(content) > 800:
        raise MemoryValidationError("content must be at most 800 characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in content):
        raise MemoryValidationError("content contains control characters")
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise MemoryValidationError("content looks like a secret and cannot be persisted")
    if any(pattern.search(content) for pattern in INJECTION_PATTERNS):
        raise MemoryValidationError("content looks like prompt injection and cannot be persisted")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError("confidence must be a number") from exc
    if not 0.0 <= confidence <= 1.0:
        raise MemoryValidationError("confidence must be in [0, 1]")

    return {
        "namespace": namespace,
        "scope": scope,
        "scope_key": "" if scope == "global_user" else scope_key,
        "kind": kind,
        "content": content,
        "provenance": provenance,
        "confidence": confidence,
    }
