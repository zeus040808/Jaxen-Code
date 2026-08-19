"""交付门禁不得把 Agent 关进一个它出不来的房间。

实测背景（2026-08-19 诊断）：`coding_bugfix_parse_bool` 一次运行里，
17 次模型请求中有 8 次是「模型交最终答案 → 门禁驳回 → 再交 → 再驳回」，
最终 205 秒撞上 episode 超时。

死锁链条：
  1. 最后一次工具调用失败 → ToolExecutor 记下 unresolved failure
  2. 工具步数耗尽 → final_only_mode = True → 工具被禁用
  3. 门禁要求「先解决那个失败」才放行最终答案
  4. 解决它需要调用工具，而工具已经不会再给了
  5. → 模型只能反复交同一份答案，直到超时

修复原则：**门禁是有预算的提醒，不是无限循环。**
模型还能动手时才驳回；动不了手时把未解决项挂在答案里交出去。
"""

from pathlib import Path

import pytest

from hh_agent_pro.agent_loop import MAX_DELIVERY_DEFERRALS
from hh_agent_pro.providers.clients import FakeModelClient
from hh_agent_pro.run_store import RunStore
from hh_agent_pro.runtime import HH, SessionStore
from hh_agent_pro.workspace import WorkspaceContext


class _NativeFake(FakeModelClient):
    """走原生工具协议的假模型，便于复现真实的 final-only 路径。"""

    supports_native_tools = True
    model = "fake-native"


def _build_agent(tmp_path, scripted, *, max_steps):
    root = Path(tmp_path) / "repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    workspace = WorkspaceContext.build(root, repo_root_override=root)
    return HH(
        model_client=_NativeFake(list(scripted)),
        workspace=workspace,
        session_store=SessionStore(root / ".hh" / "sessions"),
        run_store=RunStore(root / ".hh" / "runs"),
        approval_policy="auto",
        max_steps=max_steps,
        max_new_tokens=256,
    )


def _trace_events(agent):
    path = agent.run_store.trace_path(agent.current_run_state)
    import json

    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _count(events, name):
    return sum(1 for event in events if event.get("event") == name)


def test_exhausted_tool_budget_does_not_trap_the_final_answer(tmp_path):
    """工具用尽 + 存在未解决失败时，最终答案必须能交付出去。"""

    # 一次注定失败的 patch（old_text 不存在）耗掉唯一的工具预算，
    # 之后模型反复尝试交付。修复前这里会一直被驳回直到重试上限。
    failing_patch = (
        '<tool>{"name":"patch_file","args":{"path":"sample.txt",'
        '"old_text":"NOT-PRESENT","new_text":"x"}}</tool>'
    )
    agent = _build_agent(
        tmp_path,
        [failing_patch] + ["<final>已尽力完成。</final>"] * 8,
        max_steps=1,
    )

    final = agent.ask("修一下 sample.txt")
    events = _trace_events(agent)

    assert agent.current_run_state.status == "completed"
    assert agent.current_run_state.stop_reason == "final_answer_returned"
    # 驳回次数必须有上限，不能把预算烧光。
    assert _count(events, "completion_deferred") <= MAX_DELIVERY_DEFERRALS
    assert _count(events, "completion_forced") == 1
    # 交付了，但没有假装干净。
    assert "Unresolved at delivery" in final


def test_forced_delivery_discloses_the_unresolved_blockers(tmp_path):
    """强制交付时必须把未解决项写进答案，而不是让它们消失。"""

    failing_patch = (
        '<tool>{"name":"patch_file","args":{"path":"sample.txt",'
        '"old_text":"NOT-PRESENT","new_text":"x"}}</tool>'
    )
    agent = _build_agent(
        tmp_path,
        [failing_patch] + ["<final>完成。</final>"] * 6,
        max_steps=1,
    )

    final = agent.ask("修一下 sample.txt")

    assert "patch_file" in final, "未解决的工具失败应当在答案里点名"
    forced = [
        event for event in _trace_events(agent) if event.get("event") == "completion_forced"
    ]
    assert forced and forced[0]["reason"] == "tools_exhausted"


def test_gate_still_pushes_back_while_tools_remain(tmp_path):
    """预算还在时门禁必须照常驳回——这条护栏本身是有价值的。"""

    failing_patch = (
        '<tool>{"name":"patch_file","args":{"path":"sample.txt",'
        '"old_text":"NOT-PRESENT","new_text":"x"}}</tool>'
    )
    good_patch = (
        '<tool>{"name":"patch_file","args":{"path":"sample.txt",'
        '"old_text":"beta","new_text":"beta-fixed"}}</tool>'
    )
    agent = _build_agent(
        tmp_path,
        [failing_patch, "<final>完成。</final>", good_patch, "<final>确认已修复。</final>"],
        max_steps=6,
    )

    final = agent.ask("修一下 sample.txt")
    events = _trace_events(agent)

    # 第一次交付被驳回，模型改用一次成功的补丁清掉阻塞，然后正常交付。
    assert _count(events, "completion_deferred") == 1
    assert _count(events, "completion_forced") == 0
    assert "Unresolved at delivery" not in final
    assert (Path(agent.root) / "sample.txt").read_text(encoding="utf-8").count("beta-fixed") == 1
