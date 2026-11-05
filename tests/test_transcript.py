"""完整对话记录（transcript.jsonl）和 run_started 里的版本 / 配置信息。

问题：trace 里工具结果只留 500 字符、模型的思考内容和每轮发给模型的完整上下文都没有，
复盘时分不清"模型没看到关键报错"还是"看到了没理会"（SWE-bench sphinx-8638 就卡在这里）；
trace 里也没有模型名和代码版本，不同版本的运行混在一起时认不出来。
改法：每个 run 目录多一个 transcript.jsonl，逐轮增量记录请求和原始返回；run_started 记下版本、配置和父 run。
"""

import json
import subprocess

import pytest

from jaxen.agent import CodingAgent
from jaxen.agent_loop import agent_source_hash
from jaxen.providers import FakeModelClient
from jaxen.session_store import SessionStore
from jaxen.workspace import WorkspaceContext

LONG_LINE = "x = 1  # " + "padding " * 200  # 远超 trace 的 500 字符截断


class RecordingModel(FakeModelClient):
    """记下每轮真实发出的消息（深拷贝），并假装返回了思考内容。"""

    def __init__(self, outputs):
        super().__init__(outputs)
        self.sent = []

    def complete(self, request):
        self.sent.append(json.loads(json.dumps(request.messages)))
        self.last_reasoning_content = f"thinking #{len(self.sent)}"
        return super().complete(request)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "big.py").write_text(LONG_LINE + "\n", encoding="utf-8")
    (tmp_path / "calc.py").write_text("def average(v):\n    return sum(v) / len(v) + 1\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    return tmp_path


def make_agent(root, model, review=False):
    return CodingAgent(
        model_client=model,
        workspace=WorkspaceContext.build(root, repo_root_override=root),
        session_store=SessionStore(root / ".jaxen" / "sessions"),
        approval_policy="auto", max_steps=8, review_changes=review,
    )


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_transcript_rebuilds_every_request_and_keeps_full_output(repo):
    model = RecordingModel([
        '<tool>{"name":"read_file","args":{"path":"big.py"}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"calc.py"}}</tool>',
        "<final>done</final>",
    ])
    agent = make_agent(repo, model)
    agent.ask("look at big.py")
    entries = read_jsonl(agent.run_store.transcript_path(agent.current_run_state))

    assert [e["kind"] for e in entries] == ["request", "response"] * 3 + ["final"]
    requests = [e for e in entries if e["kind"] == "request"]
    assert "system_prompt" in requests[0] and "system_prompt" not in requests[1]  # system 不变就只写一次
    # 第三轮沿用第二轮的前缀，只写新增部分（第二轮时第一条 user 消息去掉了末尾的提醒，所以从第三轮起才复用）
    assert requests[2]["kept_messages"] > 0

    # 按 kept + new 逐轮还原，和模型真正收到的消息一模一样
    rebuilt = []
    for request, sent in zip(requests, model.sent):
        rebuilt = rebuilt[: request["kept_messages"]] + request["new_messages"]
        assert rebuilt == sent
    # 工具结果全文在里面（trace 里只有前 500 字符）
    assert LONG_LINE.strip()[:1500] in json.dumps(requests[1]["new_messages"], ensure_ascii=False)

    responses = [e for e in entries if e["kind"] == "response"]
    assert [r["reasoning_content"] for r in responses] == ["thinking #1", "thinking #2", "thinking #3"]
    assert responses[-1]["raw"] == "<final>done</final>"
    assert entries[-1]["final_answer"] == "done"


def test_model_error_is_recorded(repo):
    agent = make_agent(repo, RecordingModel([]))  # 没有输出可吐：complete() 抛错
    with pytest.raises(RuntimeError):
        agent.ask("anything")
    kinds = [e["kind"] for e in read_jsonl(agent.run_store.transcript_path(agent.current_run_state))]
    assert kinds == ["request", "model_error", "final"]


def test_run_started_records_version_config_and_parent(repo):
    patch = '<tool>{"name":"patch_file","args":{"path":"calc.py","old_text":"len(v) + 1","new_text":"len(v)"}}</tool>'
    model = RecordingModel([
        '<tool>{"name":"read_file","args":{"path":"calc.py"}}</tool>', patch,
        "<final>fixed</final>", "<final>VERDICT: approve</final>",
    ])
    agent = make_agent(repo, model, review=True)
    agent.ask("fix average")
    starts = {}
    for trace in (repo / ".jaxen" / "runs").glob("*/trace.jsonl"):
        event = next(e for e in read_jsonl(trace) if e["event"] == "run_started")
        starts[trace.parent.name] = event
    main = next(e for e in starts.values() if e["depth"] == 0)
    child = next(e for e in starts.values() if e["depth"] == 1)

    assert main["model"] == "fake" and main["provider"] == "RecordingModel"
    assert main["agent_version"] == agent_source_hash() and len(main["agent_version"]) == 12
    assert main["config"]["max_steps"] == 8 and main["config"]["review_mode"] == "always"
    assert main["parent_run_id"] is None
    assert child["parent_run_id"] == agent.current_run_state.run_id  # 审查子 agent 能直接配对到主 run
