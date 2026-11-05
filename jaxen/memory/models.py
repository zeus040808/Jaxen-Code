"""持久记忆的数据模型。

模型保持得很小：一个事实就是一个可寻址条目，而不是继续向 Markdown
尾部追加无法精确修改的自由文本。
"""

from dataclasses import asdict, dataclass


class MemoryError(ValueError):
    """所有可向工具层解释的记忆错误的基类。"""


class MemoryValidationError(MemoryError):
    pass


class MemoryNotFound(MemoryError):
    pass


class AmbiguousMemoryMatch(MemoryError):
    pass


class MemoryCapacityError(MemoryError):
    pass


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    namespace: str
    scope: str
    scope_key: str
    kind: str
    content: str
    provenance: str
    confidence: float
    created_at: str
    updated_at: str
    source_session_id: str = ""
    source_message_id: str = ""
    version: int = 1

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(
            id=str(value["id"]),
            namespace=str(value["namespace"]),
            scope=str(value["scope"]),
            scope_key=str(value.get("scope_key", "")),
            kind=str(value["kind"]),
            content=str(value["content"]),
            provenance=str(value["provenance"]),
            confidence=float(value.get("confidence", 1.0)),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            source_session_id=str(value.get("source_session_id", "")),
            source_message_id=str(value.get("source_message_id", "")),
            version=int(value.get("version", 1)),
        )
