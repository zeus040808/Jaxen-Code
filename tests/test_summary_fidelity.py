"""摘要保真度评测的判分逻辑（不调模型）：关键信息抽取、命中、编造路径。"""

from hh_agent_pro.evaluation.summary_fidelity import grade, key_facts, to_history

MESSAGES = [
    {"role": "user", "content": "Fix the bug\n\n<system-reminder>\nprogress\n</system-reminder>"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "/testbed/pkg/core.py"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "# pkg/core.py\n 1: def f(): pass"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c2", "function": {"name": "run_shell", "arguments": '{"command": "cd /testbed && python -m pytest pkg/tests/test_core.py -q"}'}}]},
    {"role": "tool", "tool_call_id": "c2", "content": "exit_code: 1\nE   AssertionError: 1 != 2"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c3", "function": {"name": "patch_file", "arguments": '{"path": "pkg/core.py", "old_text": "a", "new_text": "b"}'}}]},
    {"role": "tool", "tool_call_id": "c3", "content": "patched"},
]


def test_key_facts_come_from_tool_calls_and_failing_output():
    history = to_history(MESSAGES)
    assert "system-reminder" not in history[0]["content"]  # 每轮的提醒不进摘要输入
    facts = key_facts(history)
    assert facts == {"changed_files": ["pkg/core.py"], "test_targets": ["pkg/tests/test_core.py"],
                     "errors": ["AssertionError"], "read_files": ["pkg/core.py"]}


def test_grade_counts_hits_and_invented_paths():
    history = to_history(MESSAGES)
    summary = "1. Goal\n2. x\n3. Changed core.py; ran pkg/tests/test_core.py\n4. AssertionError fixed\n5. Also edited pkg/other.py"
    result = grade(history, summary)
    assert result["recall"]["changed_files"]["kept"] == 1  # 只写文件名也算
    assert result["recall"]["errors"]["kept"] == 1
    assert result["invented_paths"] == ["pkg/other.py"]
    assert result["sections_found"] == 5
