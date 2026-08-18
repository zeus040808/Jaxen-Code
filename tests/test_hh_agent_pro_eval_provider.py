import json

import pytest

from hh_agent_pro.evaluation.schema_v2 import fixture_digest
from hh_agent_pro.providers.clients import EvalScriptModelClient


def test_eval_script_provider_requires_explicit_enable(monkeypatch):
    monkeypatch.delenv(EvalScriptModelClient.ENABLE_ENV, raising=False)

    with pytest.raises(RuntimeError, match="eval-only"):
        EvalScriptModelClient()


def test_eval_script_provider_persists_cursor_between_process_clients(tmp_path, monkeypatch):
    script = tmp_path / "script.json"
    state = tmp_path / "state.json"
    script.write_text(
        json.dumps({"outputs": ["<final>one</final>", "<final>two</final>"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(EvalScriptModelClient.ENABLE_ENV, "1")
    monkeypatch.setenv(EvalScriptModelClient.SCRIPT_ENV, str(script))
    monkeypatch.setenv(EvalScriptModelClient.STATE_ENV, str(state))

    first = EvalScriptModelClient()
    assert first.complete("first prompt", 64) == "<final>one</final>"
    assert first.last_completion_metadata["input_tokens"] == 2

    second = EvalScriptModelClient()
    assert second.complete("second prompt", 64) == "<final>two</final>"
    assert json.loads(state.read_text(encoding="utf-8")) == {"cursor": 2}


def test_fixture_digest_ignores_runtime_python_cache(tmp_path):
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    before = fixture_digest(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "source.cpython-313.pyc").write_bytes(b"runtime cache")

    assert fixture_digest(tmp_path) == before
