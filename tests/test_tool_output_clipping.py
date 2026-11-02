"""工具输出截断必须保留结尾。

问题：工具结果原先只保留开头（工具层 12000 字符、进 prompt 时 2600 字符）。
pytest 的 FAILED 行和汇总、编译器最后的报错都在结尾，正好被截掉，
模型看不到测试为什么失败，只能猜。
"""

import json
import sys
from pathlib import Path

from hh_agent_pro.context_manager import MESSAGE_CHAR_LIMIT
from hh_agent_pro.providers import FakeModelClient
from hh_agent_pro.run_store import RunStore
from hh_agent_pro.agent import CodingAgent
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.workspace import WorkspaceContext, clip_head_tail


def test_short_text_is_unchanged():
    assert clip_head_tail("abc", 10) == "abc"


def test_keeps_head_and_tail_within_limit():
    text = "HEAD\n" + "x" * 5000 + "\nTAIL"

    clipped = clip_head_tail(text, 200)

    assert clipped.startswith("HEAD\n")
    assert clipped.endswith("\nTAIL")
    assert "omitted" in clipped
    # 标记也计入上限，调用方的上下文预算不会被撑破。
    assert len(clipped) <= 200


def test_limit_holds_across_digit_boundaries():
    for size in (995, 1000, 1005, 10_000, 100_003):
        assert len(clip_head_tail("y" * size, 120)) <= 120


def test_tiny_limit_falls_back_to_head():
    assert clip_head_tail("abcdefghij" * 20, 30) == ("abcdefghij" * 20)[:30]


def _build_agent(tmp_path, scripted):
    root = Path(tmp_path) / "repo"
    root.mkdir(parents=True, exist_ok=True)
    lines = [f"line {index}" for index in range(1, 8001)] + ["FAILED test_x - AssertionError"]
    (root / "big.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    workspace = WorkspaceContext.build(root, repo_root_override=root)
    return CodingAgent(
        model_client=FakeModelClient(list(scripted)),
        workspace=workspace,
        session_store=SessionStore(root / ".hh" / "sessions"),
        run_store=RunStore(root / ".hh" / "runs"),
        approval_policy="auto",
        max_steps=4,
    )


def test_tool_result_keeps_failure_line_at_the_end(tmp_path):
    # shell 输出走头尾截断；read_file 改为按行截断（见 test_edit_tools.py）。
    command = f'"{sys.executable}" -c "print(open(\'big.log\').read())"'
    agent = _build_agent(
        tmp_path,
        [
            json.dumps({"name": "run_shell", "args": {"command": command, "timeout": 30}}).join(["<tool>", "</tool>"]),
            "<final>done</final>",
        ],
    )

    agent.ask("show the log")

    tool_items = [item for item in agent.session["history"] if item.get("role") == "tool"]
    assert tool_items
    content = tool_items[-1]["content"]
    assert "omitted" in content
    assert "FAILED test_x - AssertionError" in content


def test_native_history_keeps_tool_result_tail(tmp_path):
    agent = _build_agent(tmp_path, ["<final>done</final>"])
    long_result = "exit_code: 1\n" + "noise\n" * 10000 + "1 failed, 3 passed"
    agent.session["history"] = [
        {"role": "user", "content": "run tests"},
        {
            "role": "assistant",
            "content": "",
            "tool_call": {"id": "call_1", "name": "run_shell", "args": {"command": "pytest -q"}},
        },
        {"role": "tool", "name": "run_shell", "args": {"command": "pytest -q"},
         "content": long_result, "tool_call_id": "call_1"},
    ]

    messages = agent.context_manager.transcript_messages()

    tool_message = next(message for message in messages if message.get("role") == "tool")
    assert tool_message["content"].startswith("exit_code: 1")
    assert tool_message["content"].endswith("1 failed, 3 passed")
    assert len(tool_message["content"]) <= MESSAGE_CHAR_LIMIT
