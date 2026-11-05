"""P1-b：一轮返回的多个工具调用按顺序全部执行。

问题：批次里只要有写入或 shell，整批就被拒绝并要求"逐个调用"，
白白多一轮模型往返，模型还得把同样的调用重发一遍。
"""

import json
import sys
from pathlib import Path

from jaxen.providers import FakeModelClient
from jaxen.run_store import RunStore
from jaxen.agent import CodingAgent
from jaxen.session_store import SessionStore
from jaxen.workspace import WorkspaceContext


def _build_agent(tmp_path, scripted, **kwargs):
    root = Path(tmp_path) / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    workspace = WorkspaceContext.build(root, repo_root_override=root)
    return CodingAgent(
        model_client=FakeModelClient(list(scripted)),
        workspace=workspace,
        session_store=SessionStore(root / ".jaxen" / "sessions"),
        run_store=RunStore(root / ".jaxen" / "runs"),
        **{"approval_policy": "auto", **kwargs},
    )


def _batch(*calls):
    actions = [{"id": f"call_{index}", "name": name, "args": args} for index, (name, args) in enumerate(calls)]
    return "<tool_batch>" + json.dumps(actions) + "</tool_batch>"


def _tool_items(agent):
    return [item for item in agent.session["history"] if item.get("role") == "tool"]


def test_mixed_batch_runs_every_call_in_order(tmp_path):
    check = f'"{sys.executable}" -c "print(open(\'a.txt\').read())"'
    agent = _build_agent(tmp_path, [
        _batch(
            ("read_file", {"path": "a.txt"}),
            ("patch_file", {"path": "a.txt", "old_text": "alpha", "new_text": "beta"}),
            ("run_shell", {"command": check, "timeout": 30}),
        ),
        "<final>done</final>",
    ])

    assert agent.ask("change alpha to beta") == "done"

    items = _tool_items(agent)
    assert [item["name"] for item in items] == ["read_file", "patch_file", "run_shell"]
    assert [item["tool_call_id"] for item in items] == ["call_0", "call_1", "call_2"]
    assert "beta" in items[2]["content"]
    assert agent.current_run_state.tool_steps == 3
    assert (agent.root / "a.txt").read_text(encoding="utf-8") == "beta\n"


def test_calls_after_a_stopping_call_are_not_executed_but_answered(tmp_path):
    agent = _build_agent(
        tmp_path,
        [
            _batch(
                ("write_file", {"path": "new.txt", "content": "x"}),
                ("read_file", {"path": "a.txt"}),
            ),
            "<final>unreachable</final>",
        ],
        read_only=True,
    )

    final = agent.ask("create new.txt")

    assert final.startswith("Blocked safely")
    items = _tool_items(agent)
    # 每个 tool_call 都有对应结果，原生消息才不会出现孤立的调用。
    assert [item["tool_call_id"] for item in items] == ["call_0", "call_1"]
    assert items[1]["content"].startswith("not executed")
    assert not (agent.root / "new.txt").exists()
