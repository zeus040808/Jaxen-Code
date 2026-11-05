"""脚本化的确定性模型：单元测试与 CI 评测用，不访问网络。"""

import json
import os
from pathlib import Path

from .base import DEFAULT_MODEL_LIMITS, ModelRequest


class FakeModelClient:
    """按顺序吐出预设输出的单元测试替身。`prompts` 记录每次请求的纯文本，便于断言。"""

    model = "fake"
    context_window = DEFAULT_MODEL_LIMITS["context_window"]
    max_output_tokens = DEFAULT_MODEL_LIMITS["max_output_tokens"]

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.requests = []
        self.last_completion_metadata = {}
        self.last_native_assistant_message = {}

    def complete(self, request: ModelRequest):
        self.requests.append(request)
        self.prompts.append(request.text())
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


class EvalScriptModelClient:
    """评测专用的持久化脚本模型。

    与 FakeModelClient 不同，它能跨多个 JSONL 进程（每个 HITL 回合一个进程）续上脚本：
    脚本和游标放在被测工作区之外，不会污染 agent 的 diff。
    """

    ENABLE_ENV = "HH_ALLOW_EVAL_FAKE_PROVIDER"
    SCRIPT_ENV = "HH_EVAL_FAKE_SCRIPT_PATH"
    STATE_ENV = "HH_EVAL_FAKE_STATE_PATH"
    model = "scripted-eval"
    provider = "fake"
    context_window = DEFAULT_MODEL_LIMITS["context_window"]
    max_output_tokens = DEFAULT_MODEL_LIMITS["max_output_tokens"]

    def __init__(self):
        if os.environ.get(self.ENABLE_ENV) != "1":
            raise RuntimeError(
                "The fake provider is eval-only. Set HH_ALLOW_EVAL_FAKE_PROVIDER=1 "
                "inside an isolated evaluation sandbox."
            )
        script_value = os.environ.get(self.SCRIPT_ENV, "").strip()
        if not script_value:
            raise RuntimeError(f"{self.SCRIPT_ENV} is required for the fake provider")
        self.script_path = Path(script_value).resolve()
        state_value = os.environ.get(self.STATE_ENV, "").strip()
        self.state_path = (
            Path(state_value).resolve()
            if state_value
            else self.script_path.with_suffix(self.script_path.suffix + ".state.json")
        )
        self.last_completion_metadata = {}
        self.last_native_assistant_message = {}

    def _outputs(self):
        try:
            payload = json.loads(self.script_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not load eval fake script: {exc}") from exc
        outputs = payload.get("outputs") if isinstance(payload, dict) else payload
        if not isinstance(outputs, list) or not outputs:
            raise RuntimeError("eval fake script must contain a non-empty outputs list")
        return outputs

    def _cursor(self):
        if not self.state_path.exists():
            return 0
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return max(0, int(payload.get("cursor", 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not load eval fake state: {exc}") from exc

    def complete(self, request: ModelRequest):
        outputs = self._outputs()
        cursor = self._cursor()
        if cursor >= len(outputs):
            raise RuntimeError("eval fake model ran out of scripted outputs")
        item = outputs[cursor]
        if isinstance(item, str):
            text, usage = item, {}
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            text, usage = item["text"], dict(item.get("usage") or {})
        else:
            raise RuntimeError(f"eval fake output {cursor} must be a string or text object")

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"cursor": cursor + 1}, sort_keys=True), encoding="utf-8")
        # 这些计数是确定性假传输的观测值，不是付费 provider 的计费估算。
        self.last_completion_metadata = {
            "input_tokens": int(usage.get("input_tokens", len(request.text().split()))),
            "output_tokens": int(usage.get("output_tokens", len(text.split()))),
            "cached_tokens": int(usage.get("cached_tokens", 0)),
            "cache_write_tokens": int(usage.get("cache_write_tokens", 0)),
        }
        return text
