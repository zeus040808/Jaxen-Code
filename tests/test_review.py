"""审查子 agent（review.py）：交付前独立复查一轮、/review 命令、用量转发。

问题：失败题里"在症状处兜底""漏改第二个文件""需求没覆盖全"占一半，主 agent 自己复查发现不了；
现有的调查子 agent（investigate）让模型自己决定用不用，全部 SWE-bench 记录里一次都没被用过。
改法：由 harness 在交付前强制派一个只读、上下文全新的审查子 agent，有问题交回主 agent，最多一轮。
"""

import json
import subprocess

import pytest

from jaxen.agent import CodingAgent
from jaxen.cli import _review_mode, build_arg_parser
from jaxen.providers import FakeModelClient
from jaxen.review import needs_review, parse_review, workspace_diff
from jaxen.session_store import SessionStore
from jaxen.events import UsageForwardingSink
from jaxen.workspace import WorkspaceContext


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "calc.py").write_text("def average(v):\n    return sum(v) / len(v) + 1\n", encoding="utf-8")
    (tmp_path / "stats.py").write_text("from calc import average\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    return tmp_path


def make_agent(root, outputs, review=True):
    return CodingAgent(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(root, repo_root_override=root),
        session_store=SessionStore(root / ".jaxen" / "sessions"),
        approval_policy="auto", max_steps=8, review_changes=review,
    )


PATCH = '<tool>{"name":"patch_file","args":{"path":"calc.py","old_text":"len(v) + 1","new_text":"len(v)"}}</tool>'


def _trace(agent):
    text = agent.run_store.trace_path(agent.current_run_state).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.mark.parametrize("answer, verdict, issues", [
    ("Looks right.\nVERDICT: approve", "approve", []),
    ("VERDICT: issues\n- stats.py: caller also divides; update it\n- calc.py: empty list crashes", "issues",
     ["stats.py: caller also divides; update it", "calc.py: empty list crashes"]),
    ("no verdict line at all", "approve", []),  # 格式不对不卡交付
    ("VERDICT: issues", "issues", ["the reviewer reported issues without details"]),
    # matplotlib-23314：审查子 agent 自己的 search 失败了，系统在回答末尾追加的清单不是审查意见
    ("Fine.\nVERDICT: approve\n\nUnresolved at delivery:\n- unresolved tool failure search:tool_failed", "approve", []),
    ("VERDICT: issues\n- a.py: missing caller\n\nUnresolved at delivery:\n- unresolved failure search:tool_failed",
     "issues", ["a.py: missing caller"]),
])
def test_parse_review(answer, verdict, issues):
    result = parse_review(answer)
    assert (result["verdict"], result["issues"]) == (verdict, issues)


def test_workspace_diff_includes_new_files_and_skips_agent_state(repo):
    (repo / "calc.py").write_text("def average(v):\n    return sum(v) / len(v)\n", encoding="utf-8")
    (repo / "new_helper.py").write_text("X = 1\n", encoding="utf-8")
    (repo / ".jaxen").mkdir()
    (repo / ".jaxen" / "state.json").write_text("{}", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"\x00\x01bytecode")
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x00")
    diff = workspace_diff(repo)
    assert "-    return sum(v) / len(v) + 1" in diff and "new file: new_helper.py" in diff
    assert ".jaxen" not in diff
    assert "__pycache__" not in diff and "logo.png" not in diff  # 编译产物和二进制文件不交给审查者


def test_review_issues_go_back_to_the_main_agent_once(repo):
    agent = make_agent(repo, [
        PATCH,
        "<final>fixed calc.py</final>",                                     # 主 agent 想交付
        "<final>VERDICT: issues\n- stats.py: check the caller</final>",      # 审查子 agent
        '<tool>{"name":"read_file","args":{"path":"stats.py"}}</tool>',     # 主 agent 采纳意见去看
        "<final>fixed calc.py; stats.py only imports it, no change needed</final>",  # 第二次交付不再审查
    ])
    answer = agent.ask("fix average")
    assert answer == "fixed calc.py; stats.py only imports it, no change needed"
    reviews = [e for e in _trace(agent) if e["event"] == "review_completed"]
    assert len(reviews) == 1 and reviews[0]["verdict"] == "issues"
    assert reviews[0]["issues"] == ["stats.py: check the caller"]
    assert agent.model_client.outputs == []  # 刚好用完：没有第二轮审查


def test_approved_review_delivers_directly(repo):
    agent = make_agent(repo, [PATCH, "<final>fixed</final>", "<final>All good.\nVERDICT: approve</final>"])
    assert agent.ask("fix average") == "fixed"
    assert [e["verdict"] for e in _trace(agent) if e["event"] == "review_completed"] == ["approve"]


def test_no_review_without_changes_or_when_disabled(repo):
    agent = make_agent(repo, ["<final>nothing to change</final>"])
    assert agent.ask("explain average") == "nothing to change"  # 没改代码：不审查
    agent = make_agent(repo, [PATCH, "<final>fixed</final>"], review=False)
    assert agent.ask("fix average") == "fixed"  # 关掉开关：不审查


def test_review_command_reports_without_editing(repo):
    clean = make_agent(repo, [])
    assert "No uncommitted changes" in clean.ask("/review")

    (repo / "calc.py").write_text("def average(v):\n    return sum(v) / len(v)\n", encoding="utf-8")
    agent = make_agent(repo, ["<final>VERDICT: issues\n- calc.py: empty input divides by zero</final>"])
    report = agent.ask("/review")
    assert "Verdict: **issues**" in report and "divides by zero" in report
    assert (repo / "calc.py").read_text(encoding="utf-8").endswith("len(v)\n")  # 没改代码


def test_child_usage_is_forwarded_but_nothing_else():
    seen = []

    class Parent:
        def emit(self, event_type, **payload):
            seen.append(event_type)

    sink = UsageForwardingSink(Parent())
    for event in ("usage", "tool_started", "run_finished", "usage"):
        sink.emit(event, x=1)
    assert seen == ["usage", "usage"]


def test_cli_review_default_depends_on_provider():
    parser = build_arg_parser()
    assert _review_mode(parser.parse_args([])) == "auto"
    assert _review_mode(parser.parse_args(["--provider", "fake"])) == "off"
    assert _review_mode(parser.parse_args(["--review", "off"])) == "off"
    assert _review_mode(parser.parse_args(["--provider", "fake", "--review", "always"])) == "always"


SOURCE_DIFF = "diff --git a/calc.py b/calc.py\n-x\n+y\n"
DOCS_DIFF = "diff --git a/README.md b/README.md\n-x\n+y\n"
TEST_DIFF = "diff --git a/tests/test_calc.py b/tests/test_calc.py\n-x\n+y\n"


@pytest.mark.parametrize("mode, diff, expected", [
    ("off", SOURCE_DIFF, False),
    ("always", DOCS_DIFF, True),            # always：有改动就审
    ("auto", SOURCE_DIFF, True),            # auto：改了源代码才审
    ("auto", DOCS_DIFF, False),             # 只改文档：跳过
    ("auto", TEST_DIFF, False),             # 只改测试：跳过
    ("auto", "new file: pkg/util.py\nX = 1\n", True),
    ("auto", "", False),
])
def test_review_modes(mode, diff, expected):
    assert needs_review(mode, diff) is expected


def test_auto_mode_skips_docs_only_changes(repo):
    agent = make_agent(repo, [
        '<tool>{"name":"write_file","args":{"path":"NOTES.md","content":"# notes\\n"}}</tool>',
        "<final>wrote notes</final>",
    ], review="auto")
    assert agent.ask("write notes") == "wrote notes"  # 没有消耗审查用的脚本输出，也没有审查事件
    assert not [e for e in _trace(agent) if e["event"] == "review_completed"]


def test_subagent_envelope_carries_usage_and_spills_long_results(repo):
    from jaxen.subagent import MAX_INLINE_RESULT_CHARS

    long_answer = "finding " * (MAX_INLINE_RESULT_CHARS // 4)
    agent = make_agent(repo, ["<final>" + long_answer + "</final>"])
    result = agent.spawn_investigation({"task": "survey", "max_steps": 3})
    assert result.startswith("<subagent-result>") and "<kind>investigate</kind>" in result
    assert "<duration-ms>" in result and "<requests>" in result
    path = result.split("<output-file>")[1].split("</output-file>")[0]
    assert path.startswith(".jaxen/subagents/investigate-")
    assert (repo / path).read_text(encoding="utf-8").strip() == long_answer.strip()  # 完整结果在文件里
    assert len(result) < len(long_answer)  # 信封里只放截断版和摘要


def test_child_only_gets_the_parents_remaining_time(repo):
    import time

    agent = make_agent(repo, [])
    agent.run_deadline = time.monotonic() + 5  # 父 agent 只剩约 5 秒
    child = agent.readonly_child(3)
    assert 1_000 <= child.episode_timeout_ms <= 5_000


def test_review_command_over_jsonl_is_a_completed_turn_with_a_message(repo):
    import io
    from jaxen.jsonl_protocol import process_request

    (repo / "calc.py").write_text("def average(v):\n    return sum(v) / len(v)\n", encoding="utf-8")
    stream = io.StringIO()
    turn = process_request(
        {"protocolVersion": 1, "requestId": "r1", "text": "/review", "sessionId": None},
        lambda request, sink: make_agent(repo, ["<final>Fine.\nVERDICT: approve</final>"]),
        stream=stream,
    )
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert turn["status"] == "completed"
    assert any(r.get("type") == "message" and "Verdict: **approve**" in r["content"] for r in rows)


def test_child_runs_are_marked_with_depth_so_analysis_can_skip_them(repo):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "swebench"))
    from localization_metrics import is_main_agent_run

    agent = make_agent(repo, [PATCH, "<final>fixed</final>", "<final>VERDICT: approve</final>"])
    agent.ask("fix average")
    runs = []
    for trace in sorted((repo / ".jaxen" / "runs").glob("*/trace.jsonl")):
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
        runs.append(events)
    depths = sorted(next(e for e in events if e["event"] == "run_started")["depth"] for events in runs)
    assert depths == [0, 1]  # 主 agent 一个 run，审查子 agent 一个 run
    assert [is_main_agent_run(events) for events in runs].count(True) == 1
