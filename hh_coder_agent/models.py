"""Model clients. The runtime uses a simple text protocol for portability."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class OpenAIChatClient:
    def __init__(self, root: Path, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None):
        load_env(root / ".env")
        from openai import OpenAI

        self.model = model or os.getenv("HH_AGENT_MODEL") or os.getenv("LLM_MODEL_ID") or "deepseek-chat"
        key = api_key or os.getenv("HH_AGENT_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("missing HH_AGENT_API_KEY or DEEPSEEK_API_KEY")
        self.client = OpenAI(
            api_key=key,
            base_url=base_url or os.getenv("HH_AGENT_BASE_URL") or os.getenv("BASE_URL") or "https://api.deepseek.com",
        )
        self.last_metadata = {}

    def complete(self, prompt: str, max_tokens: int = 2048) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a local coding agent. Follow the requested XML protocol exactly."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
        )
        usage = getattr(response, "usage", None)
        self.last_metadata = {"model": self.model, "usage": usage.model_dump() if hasattr(usage, "model_dump") else str(usage)}
        return response.choices[0].message.content or ""


class ScriptedModel:
    """Deterministic test model."""

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.last_metadata = {"model": "scripted"}

    def complete(self, prompt: str, max_tokens: int = 2048) -> str:
        if not self.outputs:
            return "<final>no scripted output left</final>"
        return self.outputs.pop(0)

