"""判分器必须把「Agent 拼错请求」和「Provider 挂了」分开。

前者是 harness 自己的缺陷，必须留在能力分母里；后者才允许被排除。
把两者混为一谈会系统性虚高能力通过率——2026-08-19 那轮评测就踩了这个坑：
三次 DeepSeek thinking 协议 HTTP 400 被记成 infra_error，
使 Coding 通过率从 62.07% 被抬到 69.23%。
"""

from jaxen.evaluation.schema_v2 import (
    EXCLUDED_FROM_CAPABILITY_DENOMINATOR,
    PASSING_TRIAL_STATUSES,
    TRIAL_STATUSES,
    classify_trial_v2,
)


def _task(**overrides):
    task = {
        "id": "attribution_probe",
        "expected_outcome": "full_completion",
        "risk": "medium",
        "category": "bugfix",
        "human_review": {"required": False},
        "budgets": {"max_steps": 6},
    }
    task.update(overrides)
    return task


def _model_error_row(message):
    return {
        "status": "failed",
        "stop_reason": "model_error",
        "final_answer": f"Model request failed: {message}",
        "checks": [],
        "changed_paths": [],
    }


# 真实捕获于 .jaxen/runs/run_20260813-093833-395ef5/run_state.json
DEEPSEEK_THINKING_400 = (
    'OpenAI-chat-compatible request failed with HTTP 400: {"error":{"message":'
    '"The `reasoning_content` in the thinking mode must be passed back to the API.",'
    '"type":"invalid_request_error","param":null,"code":"invalid_request_error"}}'
)


def test_agent_protocol_error_is_a_declared_status_that_still_counts():
    assert "agent_protocol_error" in TRIAL_STATUSES
    # 既不算通过，也不能被移出分母。
    assert "agent_protocol_error" not in PASSING_TRIAL_STATUSES
    assert "agent_protocol_error" not in EXCLUDED_FROM_CAPABILITY_DENOMINATOR


def test_malformed_request_rejected_by_provider_stays_in_denominator():
    result = classify_trial_v2(_task(), _model_error_row(DEEPSEEK_THINKING_400))

    assert result["trial_status"] == "agent_protocol_error"
    assert result["valid_capability_trial"] is True
    assert result["automated_gate_pass"] is False


def test_provider_outage_is_still_excluded_from_denominator():
    for message in (
        "request failed with HTTP 402: insufficient balance",
        "Could not reach the OpenAI-chat-compatible backend.",
        "connection reset by peer",
    ):
        result = classify_trial_v2(_task(), _model_error_row(message))

        assert result["trial_status"] == "infra_error", message
        assert result["valid_capability_trial"] is False, message


def test_unclassified_model_error_defaults_to_infra_error():
    """未识别的模型错误仍按基础设施处理，避免把 provider 故障记成能力失败。"""

    result = classify_trial_v2(_task(), _model_error_row("socket hang up"))

    assert result["trial_status"] == "infra_error"
    assert result["valid_capability_trial"] is False


def test_ordinary_functional_failure_is_unaffected():
    row = {
        "status": "completed",
        "stop_reason": "final_answer_returned",
        "final_answer": "done",
        "checks": [{"id": "unit", "passed": False}],
        "changed_paths": [],
    }

    result = classify_trial_v2(_task(), row)

    assert result["trial_status"] == "functional_failed"
    assert result["valid_capability_trial"] is True


def test_insufficient_balance_is_not_mistaken_for_a_protocol_error():
    """DeepSeek 对 402 也返回 code=invalid_request_error。

    判据必须是 HTTP 状态码；用响应体的 code 字段会把余额不足
    误判成 Agent 缺陷，从而虚低能力通过率并掩盖真实的付费问题。
    """
    body = (
        'request failed with HTTP 402: {"error":{"message":"Insufficient Balance",'
        '"type":"unknown_error","param":null,"code":"invalid_request_error"}}'
    )

    result = classify_trial_v2(_task(), _model_error_row(body))

    assert result["trial_status"] == "infra_error"
    assert result["valid_capability_trial"] is False
