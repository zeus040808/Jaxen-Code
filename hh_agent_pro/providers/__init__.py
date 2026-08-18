"""Model provider adapters."""

from .clients import (
    AnthropicCompatibleModelClient,
    EvalScriptModelClient,
    FakeModelClient,
    OllamaModelClient,
    OpenAIChatCompatibleModelClient,
    OpenAICompatibleModelClient,
)
from .codex_cli import CodexCliModelClient

__all__ = [
    "AnthropicCompatibleModelClient",
    "EvalScriptModelClient",
    "FakeModelClient",
    "OllamaModelClient",
    "OpenAIChatCompatibleModelClient",
    "OpenAICompatibleModelClient",
    "CodexCliModelClient",
]
