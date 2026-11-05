"""模型后端：OpenAI Chat Completions 兼容接口、Anthropic Messages 兼容接口，以及测试/评测用的脚本化模型。"""

from .anthropic_messages import AnthropicMessagesClient
from .base import MODEL_LIMITS, ModelRequest, model_limits
from .openai_chat import OpenAIChatClient
from .scripted import EvalScriptModelClient, FakeModelClient

__all__ = [
    "AnthropicMessagesClient",
    "EvalScriptModelClient",
    "FakeModelClient",
    "MODEL_LIMITS",
    "ModelRequest",
    "OpenAIChatClient",
    "model_limits",
]
