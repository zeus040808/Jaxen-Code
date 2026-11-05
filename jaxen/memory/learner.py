"""从用户明确表达中保守地捕获长期事实。

这不是开放式 LLM 摘要器：只有显式“记住”或稳定偏好句式才会命中，
从而避免把“帮我修这个 bug”误存成跨会话事实。
"""

from __future__ import annotations

import re

from .models import MemoryError


EXPLICIT_PATTERNS = (
    re.compile(r"(?:请)?(?:长期)?记住[：:,，\s]*(.{2,220})", re.S),
    re.compile(r"(?i)\b(?:please\s+)?remember(?:\s+that)?[\s,:]+(.{2,220})", re.S),
)
PREFERENCE_PATTERNS = (
    re.compile(r"((?:我|用户)(?:更)?(?:喜欢|偏好|习惯).{2,180})", re.S),
    re.compile(r"(?i)\b(I (?:prefer|like|usually want).{2,180})", re.S),
)
TRANSIENT_MARKERS = ("这次", "当前任务", "现在帮我", "本轮", "临时", "today only", "this time")
GENERIC_REFERENCES = re.compile(
    r"(?i)^(?:the\s+)?(?:key fact|exact line|result|answer|thing|this|that|it|这个|这件事|上述内容)[。.!！]?$"
)


class LongTermMemoryLearner:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _clean(value):
        return " ".join(str(value).strip().split()).rstrip("。.!！")

    def extract(self, user_message):
        text = str(user_message or "").strip()
        candidates = []
        for pattern in EXPLICIT_PATTERNS:
            match = pattern.search(text)
            if match:
                candidates.append((self._clean(match.group(1)), "explicit"))
                break
        for pattern in PREFERENCE_PATTERNS:
            match = pattern.search(text)
            if match:
                candidates.append((self._clean(match.group(1)), "preference"))
                break

        result = []
        seen = set()
        for content, detected_kind in candidates:
            if (
                not content
                or len(content) > 220
                or GENERIC_REFERENCES.fullmatch(content)
                or any(marker in content.casefold() for marker in TRANSIENT_MARKERS)
            ):
                continue
            key = content.casefold()
            if key in seen:
                continue
            seen.add(key)
            preference = detected_kind == "preference" or bool(
                re.search(r"(?i)(偏好|喜欢|习惯|prefer|communication|回答风格)", content)
            )
            result.append(
                {
                    "action": "add",
                    "namespace": "user" if preference else "memory",
                    "scope": "global_user" if preference else "workspace",
                    "kind": "preference" if preference else "fact",
                    "content": content,
                    "provenance": "explicit_user",
                    "confidence": 1.0,
                }
            )
        return result

    def capture(self, user_message, session_id=""):
        operations = self.extract(user_message)
        if not operations:
            return {"status": "skipped", "reason": "no_explicit_durable_fact", "results": []}
        for operation in operations:
            operation["source_session_id"] = str(session_id)
        try:
            results = self.store.apply_batch(operations)
        except MemoryError as exc:
            return {"status": "rejected", "reason": str(exc), "results": []}
        changed = [item for item in results if item.get("status") == "added"]
        return {
            "status": "captured" if changed else "duplicate",
            "results": results,
        }
