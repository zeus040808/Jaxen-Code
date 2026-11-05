"""模型调用的统一输入，以及已知模型的窗口大小。

所有 provider 只实现一个方法：`complete(request: ModelRequest) -> str`。
返回值是 HH 的内部协议文本（`<tool>`/`<tool_batch>`/`<final>` 等），由 AgentLoop 解析。
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

# 已知模型的上下文窗口与单次输出上限（token），来自各家 /models 接口。
# 未登记的模型用 DEFAULT_MODEL_LIMITS，可用 --context-window 覆盖。
MODEL_LIMITS = {
    "deepseek-flash": {"context_window": 1_048_576, "max_output_tokens": 393_216},
    "deepseek-v4-pro": {"context_window": 1_048_576, "max_output_tokens": 393_216},
    # 公司 LLM 网关上的 Claude Opus 4.8（Anthropic Messages 兼容）；按 Opus 系列的 200k 窗口登记。
    "Claude-Opus-4.8-joybuilder": {"context_window": 200_000, "max_output_tokens": 64_000},
}
DEFAULT_MODEL_LIMITS = {"context_window": 128_000, "max_output_tokens": 16_384}


def model_limits(model, context_window=None):
    limits = dict(MODEL_LIMITS.get(str(model), DEFAULT_MODEL_LIMITS))
    if context_window:
        limits["context_window"] = int(context_window)
    return limits


@dataclass
class ModelRequest:
    system_prompt: str
    messages: list
    max_new_tokens: int
    tools: Optional[list] = None
    event_callback: Optional[Callable[[dict], None]] = field(default=None, repr=False)

    def text(self):
        """把整次请求拼成纯文本：给脚本化模型、日志和测试断言用。"""
        parts = [self.system_prompt]
        for message in self.messages:
            content = message.get("content")
            if content:
                parts.append(f"[{message.get('role', '')}] {content}")
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                parts.append(f"[tool_call {function.get('name', '')}] {function.get('arguments', '')}")
        return "\n\n".join(parts)
