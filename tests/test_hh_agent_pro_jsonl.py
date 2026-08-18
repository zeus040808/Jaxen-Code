import io
import json
import time

from hh_agent_pro.jsonl_protocol import process_request
from hh_agent_pro.runtime import HH, SessionStore
from hh_agent_pro.workspace import WorkspaceContext


class ScriptedModel:
    supports_prompt_cache = False
    model = "scripted-jsonl"

    def __init__(self, outputs, delay=0):
        self.outputs = list(outputs)
        self.delay = delay
        self.prompts = []
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        if self.delay:
            time.sleep(self.delay)
        self.last_completion_metadata = {
            "input_tokens": 11,
            "output_tokens": 3,
            "cached_tokens": 2,
        }
        return self.outputs.pop(0)


def request(request_id, text, session_id=None, responses=None):
    return {
        "protocolVersion": 1,
        "requestId": request_id,
        "text": text,
        "sessionId": session_id,
        "responses": list(responses or []),
    }


def parse_lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def make_factory(tmp_path, models, *, approval="never", timeout_ms=180000, secret_names=()):
    models = list(models)
    store = SessionStore(tmp_path / ".hh" / "sessions")

    def factory(protocol_request, sink):
        workspace = WorkspaceContext.build(str(tmp_path), repo_root_override=tmp_path)
        kwargs = {
            "model_client": models.pop(0),
            "workspace": workspace,
            "session_store": store,
            "approval_policy": approval,
            "event_sink": sink,
            "machine_mode": True,
            "episode_timeout_ms": timeout_ms,
            "secret_env_names": secret_names,
            "max_steps": 6,
            "max_new_tokens": 256,
        }
        if protocol_request["sessionId"]:
            return HH.from_session(
                session_id=protocol_request["sessionId"],
                **kwargs,
            )
        return HH(**kwargs)

    return factory


def test_jsonl_golden_event_order_and_turn_shape(tmp_path):
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    model = ScriptedModel(
        [
            '<tool>{"name":"read_file","args":{"path":"sample.py"}}</tool>',
            "<final>value is one</final>",
        ]
    )
    stream = io.StringIO()

    turn = process_request(
        request("req_golden", "inspect sample.py"),
        make_factory(tmp_path, [model]),
        stream,
    )
    lines = parse_lines(stream)
    event_types = [line["type"] for line in lines if line["kind"] == "event"]

    assert event_types == [
        "thinking",
        "usage",
        "operation.started",
        "operation.finished",
        "thinking",
        "usage",
        "message",
    ]
    assert turn["status"] == "completed"
    assert turn["requestId"] == "req_golden"
    assert turn["finalAnswer"] == "value is one"
    assert turn["usage"] == {
        "inputTokens": 22,
        "outputTokens": 6,
        "cacheReadTokens": 4,
        "cacheWriteTokens": 0,
        "requests": 2,
    }
    assert set(turn["artifactPaths"]) == {"runDir", "trace", "runState", "report"}
    assert lines[-1] == turn


def test_jsonl_same_session_two_turns_and_free_text_clarification(tmp_path):
    first_model = ScriptedModel(
        ["<needs_user_input>What timeout value should be used?</needs_user_input>"]
    )
    second_model = ScriptedModel(["<final>timeout 30 accepted</final>"])
    factory = make_factory(tmp_path, [first_model, second_model])
    first_stream = io.StringIO()
    first_turn = process_request(request("req_1", "change timeout"), factory, first_stream)
    input_event = next(
        line
        for line in parse_lines(first_stream)
        if line.get("type") == "input.requested"
    )

    second_stream = io.StringIO()
    second_turn = process_request(
        request(
            "req_2",
            "",
            first_turn["sessionId"],
            [{"inputId": input_event["inputId"], "value": "30"}],
        ),
        factory,
        second_stream,
    )

    assert first_turn["status"] == "waiting"
    assert second_turn["status"] == "completed"
    assert second_turn["sessionId"] == first_turn["sessionId"]
    assert "30" in second_model.prompts[0]


def test_jsonl_approval_persists_then_executes_exactly_once(tmp_path):
    call = '<tool>{"name":"write_file","args":{"path":"approved.txt","content":"ok\\n"}}</tool>'
    first_model = ScriptedModel([call])
    second_model = ScriptedModel(["<final>approved write completed</final>"])
    third_model = ScriptedModel(["<final>no pending action</final>"])
    factory = make_factory(
        tmp_path,
        [first_model, second_model, third_model],
        approval="ask",
    )
    first_stream = io.StringIO()
    first_turn = process_request(request("req_approve_1", "write the file"), factory, first_stream)
    approval_event = next(
        line
        for line in parse_lines(first_stream)
        if line.get("type") == "input.requested"
        and line.get("inputType") == "approval"
    )

    assert first_turn["status"] == "waiting"
    assert not (tmp_path / "approved.txt").exists()

    second_stream = io.StringIO()
    response = {"inputId": approval_event["inputId"], "decision": "approve"}
    second_turn = process_request(
        request("req_approve_2", "", first_turn["sessionId"], [response]),
        factory,
        second_stream,
    )
    assert second_turn["status"] == "completed"
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "ok\n"
    resumed_operations = [
        line
        for line in parse_lines(second_stream)
        if line.get("type") == "operation.started" and line.get("resumed")
    ]
    assert len(resumed_operations) == 1

    third_stream = io.StringIO()
    process_request(
        request("req_approve_3", "check", first_turn["sessionId"], [response]),
        factory,
        third_stream,
    )
    assert not any(
        line.get("type") == "operation.started"
        for line in parse_lines(third_stream)
    )


def test_jsonl_rejected_approval_is_not_tool_failure_or_mutation(tmp_path):
    call = '<tool>{"name":"write_file","args":{"path":"denied.txt","content":"no"}}</tool>'
    factory = make_factory(
        tmp_path,
        [ScriptedModel([call]), ScriptedModel(["<final>unused</final>"])],
        approval="ask",
    )
    first_stream = io.StringIO()
    first_turn = process_request(request("req_deny_1", "write"), factory, first_stream)
    approval_event = next(
        line for line in parse_lines(first_stream) if line.get("inputType") == "approval"
    )
    second_stream = io.StringIO()
    second_turn = process_request(
        request(
            "req_deny_2",
            "",
            first_turn["sessionId"],
            [{"inputId": approval_event["inputId"], "approved": False}],
        ),
        factory,
        second_stream,
    )

    assert second_turn["status"] == "completed"
    assert "not executed" in second_turn["finalAnswer"]
    assert not (tmp_path / "denied.txt").exists()
    assert not any(
        line.get("type") == "operation.started"
        for line in parse_lines(second_stream)
    )


def test_jsonl_redacts_secret_and_enforces_episode_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("HH_JSONL_TEST_SECRET", "TOP_SECRET_VALUE")
    secret_stream = io.StringIO()
    secret_turn = process_request(
        request("req_secret", "answer"),
        make_factory(
            tmp_path,
            [ScriptedModel(["<final>TOP_SECRET_VALUE</final>"])],
            secret_names=("HH_JSONL_TEST_SECRET",),
        ),
        secret_stream,
    )

    assert "TOP_SECRET_VALUE" not in secret_stream.getvalue()
    assert "redacted" in secret_turn["finalAnswer"].casefold()

    timeout_stream = io.StringIO()
    timeout_turn = process_request(
        request("req_timeout", "slow answer"),
        make_factory(
            tmp_path,
            [ScriptedModel(["<final>too late</final>"], delay=0.03)],
            timeout_ms=5,
        ),
        timeout_stream,
    )

    assert timeout_turn["status"] == "failed"
    assert timeout_turn["stopReason"] == "episode_timeout"
    assert any(
        line.get("type") == "error" and line.get("errorType") == "episode_timeout"
        for line in parse_lines(timeout_stream)
    )
