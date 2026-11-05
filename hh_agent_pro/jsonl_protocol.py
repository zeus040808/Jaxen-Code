"""Versioned JSONL machine protocol used by eval adapters and CI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .workspace import clip


PROTOCOL_VERSION = 1


def validate_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    if int(payload.get("protocolVersion", 0)) != PROTOCOL_VERSION:
        raise ValueError(f"protocolVersion must be {PROTOCOL_VERSION}")
    request_id = str(payload.get("requestId", "")).strip()
    if not request_id:
        raise ValueError("requestId is required")
    if not isinstance(payload.get("text", ""), str):
        raise ValueError("text must be a string")
    session_id = payload.get("sessionId")
    if session_id is not None and not str(session_id).strip():
        raise ValueError("sessionId must be null or a non-empty string")
    responses = payload.get("responses", [])
    if not isinstance(responses, list) or any(not isinstance(item, dict) for item in responses):
        raise ValueError("responses must be a list of objects")
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "requestId": request_id,
        "text": payload.get("text", ""),
        "sessionId": str(session_id) if session_id is not None else None,
        "responses": responses,
    }


class JsonlEventSink:
    """Translate internal UI events into stable eval-facing event lines."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.agent = None
        self.sequence = 0
        self.operation_count = 0
        self.operations = []
        self.usage = {
            "inputTokens": 0,
            "outputTokens": 0,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "requests": 0,
        }

    def bind_agent(self, agent):
        self.agent = agent
        return self

    def _safe(self, value):
        if self.agent is None:
            return value
        return self.agent.redact_artifact(value)

    def write(self, payload):
        self.sequence += 1
        row = {"kind": "event", "sequence": self.sequence, **self._safe(payload)}
        print(json.dumps(row, ensure_ascii=False, sort_keys=True), file=self.stream, flush=True)
        return row

    def emit(self, event_type, **payload):
        payload = self._safe(payload)
        if event_type == "tool_started":
            self.operation_count += 1
            operation_id = f"op_{self.operation_count}"
            self.operations.append((str(payload.get("name", "tool")), operation_id))
            return self.write(
                {
                    "type": "operation.started",
                    "operationId": operation_id,
                    "name": payload.get("name", "tool"),
                    "input": payload.get("args", {}),
                    "operationType": (
                        "subagent" if payload.get("name") == "delegate" else "tool"
                    ),
                    "resumed": bool(payload.get("resumed", False)),
                }
            )
        if event_type == "tool_finished":
            name = str(payload.get("name", "tool"))
            operation_id = next(
                (
                    operation_id
                    for operation_name, operation_id in reversed(self.operations)
                    if operation_name == name
                ),
                "",
            )
            return self.write(
                {
                    "type": "operation.finished",
                    "operationId": operation_id,
                    "name": name,
                    "output": clip(str(payload.get("result", "")), 4000),
                    "metadata": payload.get("metadata", {}),
                    "resumed": bool(payload.get("resumed", False)),
                }
            )
        if event_type in {"needs_user_input", "approval_requested"}:
            return self.write(
                {
                    "type": "input.requested",
                    "inputId": payload.get("input_id", ""),
                    "inputType": (
                        "approval"
                        if event_type == "approval_requested"
                        else payload.get("input_kind", "text")
                    ),
                    "question": payload.get(
                        "question",
                        f"Approve {payload.get('name', 'the pending action')}?",
                    ),
                    "name": payload.get("name", ""),
                    "arguments": payload.get("args", {}),
                }
            )
        if event_type == "compaction":
            return self.write({"type": "compaction", **payload})
        if event_type == "usage":
            self.usage["inputTokens"] += int(payload.get("input_tokens", 0) or 0)
            self.usage["outputTokens"] += int(payload.get("output_tokens", 0) or 0)
            self.usage["cacheReadTokens"] += int(payload.get("cache_read_tokens", 0) or 0)
            self.usage["cacheWriteTokens"] += int(payload.get("cache_write_tokens", 0) or 0)
            self.usage["requests"] += int(payload.get("requests", 0) or 0)
            return self.write({"type": "usage", "usage": dict(self.usage)})
        if event_type == "error":
            return self.write(
                {
                    "type": "error",
                    "errorType": payload.get("error_type", "runtime_error"),
                    "message": payload.get("message", ""),
                }
            )
        if event_type in {"status", "provider_status", "reasoning_summary"}:
            text = payload.get("text") or payload.get("message") or ""
            if text:
                return self.write(
                    {
                        "type": "thinking",
                        "text": clip(str(text), 2000),
                        "source": event_type,
                    }
                )
            return None
        if event_type == "run_finished":
            final = str(payload.get("final_answer", "")).strip()
            if final and payload.get("status") != "waiting_for_user":
                return self.write(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": final,
                    }
                )
        return None


def _turn_payload(agent, final_answer, sink, request_id):
    run_state = agent.current_run_state
    status = "failed"
    if run_state is None and final_answer:
        status = "completed"  # /review 这类命令不进主循环、没有运行状态，但有完整的回答
    elif run_state is not None and run_state.status == "completed":
        status = "completed"
    elif run_state is not None and run_state.status == "waiting_for_user":
        status = "waiting"
    run_dir = Path(agent.current_run_dir) if agent.current_run_dir else None
    artifact_paths = {}
    if run_dir is not None:
        artifact_paths = {
            "runDir": str(run_dir),
            "trace": str(run_dir / "trace.jsonl"),
            "runState": str(run_dir / "run_state.json"),
            "report": str(run_dir / "report.json"),
        }
    payload = {
        "kind": "turn",
        "protocolVersion": PROTOCOL_VERSION,
        "requestId": request_id,
        "status": status,
        "sessionId": str(agent.session["id"]),
        "runId": str(run_state.run_id if run_state else ""),
        "finalAnswer": agent.redact_text(str(final_answer)),
        "usage": dict(sink.usage),
        "artifactPaths": artifact_paths,
    }
    if run_state is not None:
        payload["stopReason"] = str(run_state.stop_reason)
        if run_state.stop_reason == "model_error":
            payload["failureKind"] = "infra_error"
        elif run_state.stop_reason == "episode_timeout":
            payload["failureKind"] = "functional_failed"
    return payload


def process_request(request, agent_factory, stream=None):
    stream = stream or sys.stdout
    request = validate_request(request)
    sink = JsonlEventSink(stream)
    agent = agent_factory(request, sink)
    sink.bind_agent(agent)
    final_answer = ""
    try:
        final_answer = agent.ask(
            request["text"],
            responses=request["responses"],
            request_id=request["requestId"],
        )
    except RuntimeError as exc:
        final_answer = str(exc)
    if agent.current_run_state is None and final_answer:
        # 没走主循环（如 /review）：没有 run_finished 事件，这里补发回答，界面才显示得出来
        sink.write({"type": "message", "role": "assistant", "content": agent.redact_text(str(final_answer))})
    turn = _turn_payload(agent, final_answer, sink, request["requestId"])
    print(json.dumps(turn, ensure_ascii=False, sort_keys=True), file=stream, flush=True)
    return turn


def run_jsonl_stdio(args, build_agent):
    """Read one or more request lines; each line may resume a saved session."""

    exit_code = 0
    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        try:
            request = validate_request(json.loads(raw_line))
            request_args = argparse.Namespace(**vars(args))
            request_args.resume = request["sessionId"]

            def factory(_request, sink):
                return build_agent(
                    request_args,
                    event_sink=sink,
                    machine_mode=True,
                )

            turn = process_request(request, factory, stream=sys.stdout)
            if turn["status"] == "failed":
                exit_code = 1
        except Exception as exc:
            message = str(exc)
            print(
                json.dumps(
                    {
                        "kind": "event",
                        "sequence": 1,
                        "type": "error",
                        "errorType": exc.__class__.__name__,
                        "message": message,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            print(
                json.dumps(
                    {
                        "kind": "turn",
                        "protocolVersion": PROTOCOL_VERSION,
                        "requestId": "",
                        "status": "failed",
                        "sessionId": None,
                        "runId": "",
                        "finalAnswer": message,
                        "usage": {},
                        "artifactPaths": {},
                        "failureKind": "infra_error",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            exit_code = 1
    return exit_code
