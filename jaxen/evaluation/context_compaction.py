"""上下文压缩模块评测（假模型，零成本，可复现）。

每次发给模型的请求都必须满足这些不变量（程序检查，不用模型判断）：
    within_budget   估算 token ≤ 预算
    protocol        每个工具调用都紧跟着它的全部结果，没有孤立的 tool_call id；需要时思考内容原样回传
    goal_present    用户的原始请求还在（消息里或提醒里）
    latest_intact   最新一条工具结果原文还在
    system_stable   system 不变（前缀缓存）
    history_intact  session 里的完整历史没被删改（断点恢复、会话检索靠它）
另外按位置统计"标记事实"（FACT: xxx）压缩后还在不在。

A 部分直接调用 ContextManager.prepare()，用造出来的历史精确控制占预算的比例；
B 部分让真实 agent 跑长任务（复用故障注入的假服务器），逐个检查每一次请求。
摘要由假模型来写：把历史里带 FACT: 的行原样抄进摘要（相当于一个忠实的摘要器），
测的是"摘要能否正确进入后续请求"这条流程，不测摘要写得好不好。
"""

import copy
import json
import re
import shutil
import tempfile
from pathlib import Path

from ..agent import CodingAgent
from ..context_manager import SUMMARY_SYSTEM_PROMPT
from ..providers import OpenAIChatClient, openai_chat
from ..run_store import RunStore
from ..session_store import SessionStore
from ..workspace import WorkspaceContext
from .fault_injection import (
    FIXTURE, PATCH, READ, RUN_TESTS, TASK, FakeServer, _isolated_process_env, _sse, _Stream, final, tool,
)

SCHEMA_VERSION = "context-compaction-v1"
GOAL = "GOAL-7f3a: fix the date parser in parser.py and keep the public API unchanged"
FACT = re.compile(r"FACT: [\w-]+")


def _filler(chars):
    line = "x = compute_something(value, other_value)  # filler line\n"
    return (line * (chars // len(line) + 1))[:chars]


class FactSummarizer:
    """假的摘要模型：把请求里所有 FACT: 行抄进摘要；fail=True 时报错（模拟摘要失败）。"""

    model = "fact-summarizer"
    context_window = 1_048_576
    max_output_tokens = 8192

    def __init__(self, fail=False, requires_reasoning=False):
        self.fail = fail
        self.requires_reasoning_content_for_tool_calls = requires_reasoning
        self.calls = 0
        self.last_completion_metadata = {}
        self.last_native_assistant_message = {}

    def complete(self, request):
        self.calls += 1
        if self.fail:
            raise RuntimeError("summary backend unavailable")
        facts = sorted(set(FACT.findall(json.dumps(request.messages, ensure_ascii=False))))
        return "1. User request and goals\n(see request)\n3. Files read\n" + "\n".join(facts) + "\n5. Current state and next steps\ncontinue"


# ---- 不变量 ------------------------------------------------------------------


def _protocol_ok(messages, needs_reasoning):
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get("role") == "tool":
            return False  # 前面没有对应的 assistant 工具调用
        if message.get("role") == "assistant" and message.get("tool_calls"):
            if needs_reasoning and not message.get("reasoning_content"):
                return False
            ids = {call["id"] for call in message["tool_calls"]}
            seen = set()
            while index + 1 < len(messages) and messages[index + 1].get("role") == "tool":
                index += 1
                seen.add(messages[index].get("tool_call_id"))
            if seen != ids:
                return False
        index += 1
    return True


def check_request(agent, system_prompt, messages, *, goal, history_before=None, system_before=None, budget=None):
    manager = agent.context_manager
    text = "\n".join(str(message.get("content") or "") for message in messages)  # 原文比对：json 会转义换行
    history = agent.session.get("history", [])
    tool_items = [item for item in history if item.get("role") == "tool"]
    latest = str(tool_items[-1].get("content", "")) if tool_items else ""
    checks = {
        "within_budget": manager.estimate(system_prompt, messages) <= (budget or manager.budget_tokens()),
        "protocol": _protocol_ok(messages, bool(getattr(agent.model_client, "requires_reasoning_content_for_tool_calls", False))),
        "goal_present": goal in text,
        "latest_intact": not latest or latest[-2000:] in text,
    }
    if system_before is not None:
        checks["system_stable"] = system_prompt == system_before
    if history_before is not None:
        checks["history_intact"] = history == history_before
    return checks


# ---- A 部分：直接调用 prepare() ------------------------------------------------


def _make_agent(root, summarizer, flags=None):
    return CodingAgent(model_client=summarizer, workspace=WorkspaceContext.build(root, repo_root_override=root),
                       session_store=SessionStore(root / ".jaxen" / "sessions"), run_store=RunStore(root / ".jaxen" / "runs"),
                       approval_policy="auto", feature_flags=flags)


def _exchange(agent, index, content, *, parallel=1, reasoning=""):
    calls = [{"id": f"call_{index}_{k}", "name": "read_file", "args": {"path": f"f{index}_{k}.py"}} for k in range(parallel)]
    item = {"role": "assistant", "content": ""}
    item.update({"tool_calls": calls} if parallel > 1 else {"tool_call": calls[0]})
    if reasoning:
        item["reasoning_content"] = reasoning
    agent.record(item)
    for k, call in enumerate(calls):
        agent.record({"role": "tool", "name": "read_file", "args": call["args"], "tool_call_id": call["id"],
                      "content": content if k == 0 else f"FACT: par-{index}-{k}\n" + _filler(1200)})


def _set_ratio(agent, ratio, goal):
    """把预算设成"当前请求恰好占预算的 ratio"，从而精确落在某个压缩档位。"""
    manager = agent.context_manager
    total = manager.estimate(manager.system_prompt(), manager._with_reminder(goal))
    manager.working_budget_tokens = int(total / ratio)


def _a_scenario(sid, *, exchanges=24, size=3000, ratio, kind, kept=(), lost=(), fail_summary=False, flags=None,
                parallel=1, reasoning=False, recent_size=None, rounds=1, calibrate=None, long_goal=False, known_issue="", note=""):
    """kind: 期望的压缩方式（None 表示不压缩）。kept / lost: 期望保留 / 按设计会丢的事实前缀。"""
    return {"id": sid, "part": "A", "exchanges": exchanges, "size": size, "ratio": ratio, "kind": kind,
            "kept": list(kept), "lost": list(lost), "fail_summary": fail_summary, "flags": flags or {},
            "parallel": parallel, "reasoning": reasoning, "recent_size": recent_size, "rounds": rounds,
            "calibrate": calibrate, "long_goal": long_goal, "known_issue": known_issue, "note": note}


A_SCENARIOS = [
    _a_scenario("under_threshold", ratio=0.5, kind=None, kept=["old", "recent"], note="低于 60% 不压缩"),
    _a_scenario("light_band", ratio=0.7, kind="light", kept=["recent"], lost=["old"],
                note="60%–80%：较早的大块工具输出换成占位符，最近 8 条原文保留"),
    _a_scenario("summary_band", ratio=0.9, kind="summary", kept=["old", "recent"], note="超过 80%：较早的轮次进摘要"),
    _a_scenario("summary_twice", ratio=0.9, kind="summary", kept=["old", "recent"], rounds=2,
                note="第二次摘要要把第一次的摘要带进去：最早的事实仍在"),
    _a_scenario("summary_fails_then_trim", ratio=3.0, kind="light+trim", kept=["recent"], lost=["old"], fail_summary=True,
                note="摘要失败 → 轻压缩 + 兜底裁剪；用户的原始请求必须还在"),
    _a_scenario("summary_disabled_then_trim", ratio=3.0, kind="light+trim", kept=["recent"], lost=["old"],
                flags={"context_summary": False}, note="关掉摘要功能时同样走兜底裁剪"),
    _a_scenario("long_request_then_trim", ratio=3.0, kind="light+trim", kept=["recent"], lost=["old"], fail_summary=True,
                long_goal=True, note="用户请求超过 1000 字符（SWE-bench 的 issue 常见）：兜底裁剪后请求全文必须还在"),
    _a_scenario("huge_recent_outputs", exchanges=12, size=1000, recent_size=9000, ratio=0.95, kind="summary",
                kept=["recent"], note="最近几条本身很大，轻压缩救不了"),
    _a_scenario("parallel_calls", parallel=3, ratio=0.9, kind="summary", kept=["recent"],
                note="一次调用多个工具：调用和结果不能被拆开"),
    _a_scenario("reasoning_replay", reasoning=True, ratio=0.9, kind="summary", kept=["recent"],
                note="DeepSeek thinking：保留下来的工具调用必须带原样的思考内容"),
    _a_scenario("calibration_triggers_earlier", ratio=0.5, kind="summary", kept=["old", "recent"], calibrate=2.0,
                note="API 报告的真实 token 是估算的 2 倍：同样的历史应当提前进入摘要"),
]


def run_a(spec):
    workdir = Path(tempfile.mkdtemp(prefix="jaxen-ctx-"))
    try:
        root = workdir / "repo"
        root.mkdir()
        (root / "parser.py").write_text("def parse(text):\n    return text\n", encoding="utf-8")
        summarizer = FactSummarizer(fail=spec["fail_summary"], requires_reasoning=spec["reasoning"])
        agent = _make_agent(root, summarizer, spec["flags"])
        goal = GOAL + (" Details: " + _filler(2400) + " GOAL-END-7f3a" if spec["long_goal"] else "")
        agent.progress.begin(goal, "run_eval")  # 和真实运行一样：任务状态里记下目标，提醒里会带上
        agent.record({"role": "user", "content": goal})
        rows = []
        for round_index in range(spec["rounds"]):
            for i in range(spec["exchanges"]):
                index = round_index * 100 + i
                recent = i >= spec["exchanges"] - 4
                where = "recent" if recent and round_index == spec["rounds"] - 1 else "old"
                size = spec["recent_size"] if recent and spec["recent_size"] else spec["size"]
                _exchange(agent, index, f"FACT: {where}-{index}\n" + _filler(size), parallel=spec["parallel"],
                          reasoning=f"thinking about f{index}" if spec["reasoning"] else "")
            _set_ratio(agent, spec["ratio"], goal)
            if spec["calibrate"]:  # 连续几轮 API 都报告真实用量是估算的 N 倍，校准系数收敛到 N
                manager = agent.context_manager
                raw = manager.estimate(manager.system_prompt(), manager._with_reminder(goal)) / manager.calibration
                for _ in range(6):
                    manager._last_estimate = int(raw * manager.calibration)
                    manager.observe_usage({"input_tokens": int(raw * spec["calibrate"])})
            system_before = agent.context_manager.system_prompt()
            history_before = copy.deepcopy(agent.session["history"])
            request, meta = agent.context_manager.prepare(goal)
            checks = check_request(agent, request.system_prompt, request.messages, goal=goal,
                                   history_before=history_before, system_before=system_before, budget=meta["budget_tokens"])
            rows.append((request, meta, checks))
        request, meta, checks = rows[-1]
        return _grade_a(spec, request, meta, checks, [r[1] for r in rows], summarizer)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _facts(text, prefix):
    return sorted(set(m for m in FACT.findall(text) if m.startswith(f"FACT: {prefix}-")))


# old = 最早的那条事实（必定在"最近 8 条工具结果"之外）；recent = 最新的那条
def _fact_present(text, where, spec):
    if where == "old":
        return "FACT: old-0" in text
    last = (spec["rounds"] - 1) * 100 + spec["exchanges"] - 1
    return f"FACT: recent-{last}" in text


def _grade_a(spec, request, meta, checks, metas, summarizer):
    text = json.dumps(request.messages, ensure_ascii=False)
    compaction = meta.get("compaction") or {}
    kind = compaction.get("kind")
    retention = {where: _fact_present(text, where, spec) for where in spec["kept"] + spec["lost"]}
    ok_kind = kind == spec["kind"]
    ok_facts = all(retention[w] for w in spec["kept"]) and not any(retention[w] for w in spec["lost"])
    passed = ok_kind and ok_facts and all(checks.values())
    before, after = compaction.get("before_tokens"), compaction.get("after_tokens")
    return {
        "id": spec["id"], "part": "A", "note": spec["note"], "known_issue": spec["known_issue"],
        "verdict": "pass" if passed else "fail", "expected_kind": spec["kind"], "kind": kind,
        "invariants": checks, "fact_retention": retention,
        "compression_ratio": round(1 - after / before, 3) if before and after else None,
        "summary_requests": summarizer.calls, "rounds": [m.get("compaction") for m in metas],
    }


# ---- B 部分：真实 agent 跑长任务 ----------------------------------------------


class CompactionServer(FakeServer):
    """在故障注入假服务器上加两件事：回答摘要请求；逐个检查 agent 发出的请求是否满足不变量。"""

    def __init__(self, script, fail_summary=False):
        super().__init__(script)
        self.fail_summary = fail_summary
        self.agent = None
        self.summary_requests = 0
        self.request_checks = []

    def urlopen(self, request, timeout=None):
        payload = json.loads(request.data)
        system, messages = payload["messages"][0]["content"], payload["messages"][1:]
        if system == SUMMARY_SYSTEM_PROMPT:
            self.summary_requests += 1
            if self.fail_summary:
                raise openai_chat.urllib.error.HTTPError(request.full_url, 400, "summary failed", {}, None)
            facts = sorted(set(FACT.findall(json.dumps(messages, ensure_ascii=False))))
            return _Stream(_sse({"content": "1. User request and goals\n3. Files read\n" + "\n".join(facts)
                                 + "\n5. Current state and next steps\ncontinue"}, "stop"))
        self.request_checks.append(check_request(self.agent, system, messages, goal=TASK))
        return super().urlopen(request, timeout)


B_FIXTURE = {f"mod_{i}.py": f"# FACT: mod-{i}\n" + _filler(3500) for i in range(16)}
B_FIXTURE["big_output.py"] = (
    "print('FACT: head-1')\nfor i in range(3000):\n    print('noise line', i)\n    if i == 1500:\n"
    "        print('FACT: middle-1')\nprint('FACT: tail-1')\n"
)
READS = [tool("read_file", path=f"mod_{i}.py") for i in range(16)]
LONG_FIX = [*READS, READ, PATCH, RUN_TESTS, final()]


def _b_scenario(sid, script, *, budget, kinds, fail_summary=False, kept=(), lost=(), max_summary_requests=None,
                known_issue="", note=""):
    return {"id": sid, "part": "B", "script": script, "budget": budget, "kinds": list(kinds), "fail_summary": fail_summary,
            "kept": list(kept), "lost": list(lost), "max_summary_requests": max_summary_requests,
            "known_issue": known_issue, "note": note}


B_SCENARIOS = [
    _b_scenario("long_task_compacts_and_finishes", LONG_FIX, budget=7000, kinds=["summary"],
                note="16 次大文件读取 + 修复：预算很小，进入摘要压缩，任务照常完成"),
    _b_scenario("long_task_summary_fails", LONG_FIX, budget=6000, kinds=["trim"], fail_summary=True, max_summary_requests=1,
                note="摘要接口报错：退回兜底裁剪，任务照常完成，目标不丢；同一次运行只尝试一次摘要"),
    _b_scenario("huge_command_output", [READ, PATCH, RUN_TESTS, tool("run_shell", command="python big_output.py"), final()],
                budget=60000, kinds=[], kept=["head", "tail"], lost=["middle"],
                note="命令输出 4 万多字符：执行时保留头尾、丢中间（衡量是否需要\"大结果存磁盘\"）"),
]


def run_b(spec):
    workdir = Path(tempfile.mkdtemp(prefix="jaxen-ctx-"))
    root = workdir / "repo"
    root.mkdir()
    try:
        for rel, content in {**FIXTURE, **B_FIXTURE}.items():
            (root / rel).write_text(content, encoding="utf-8")
        server = CompactionServer(spec["script"], fail_summary=spec["fail_summary"])
        client = OpenAIChatClient("deepseek-flash", "https://fake.invalid", "fake-key", 0.0, 5, label="Fake")
        client.sleep = lambda seconds: None
        agent = CodingAgent(model_client=client, workspace=WorkspaceContext.build(root, repo_root_override=root),
                            session_store=SessionStore(root / ".jaxen" / "sessions"), run_store=RunStore(root / ".jaxen" / "runs"),
                            approval_policy="auto", max_steps=30, max_new_tokens=1024, toolset="core")
        agent.context_manager.working_budget_tokens = spec["budget"]
        server.agent = agent
        original = openai_chat.urllib.request.urlopen
        openai_chat.urllib.request.urlopen = server.urlopen
        error = ""
        try:
            with _isolated_process_env():
                agent.ask(TASK)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"[:300]
        finally:
            openai_chat.urllib.request.urlopen = original
        return _grade_b(spec, agent, server, error)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _grade_b(spec, agent, server, error):
    run_state = agent.current_run_state
    stop_reason = run_state.stop_reason if run_state else "no_run"
    trace_path = agent.run_store.trace_path(run_state) if run_state else None
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()] if trace_path else []
    kinds = [e.get("kind") for e in events if e.get("event") == "checkpoint_created" and e.get("trigger") == "context_reduction"]
    transcript = agent.run_store.transcript_path(run_state).read_text(encoding="utf-8") if run_state else ""
    retention = {where: f"FACT: {where}-1" in transcript for where in spec["kept"] + spec["lost"]}
    violations = {name: sum(1 for c in server.request_checks if not c[name]) for name in server.request_checks[0]} \
        if server.request_checks else {}
    kinds_ok = all(any(k and want in k for k in kinds) for want in spec["kinds"])
    passed = (stop_reason == "final_answer_returned" and kinds_ok and not any(violations.values())
              and all(retention[w] for w in spec["kept"]) and not any(retention[w] for w in spec["lost"])
              and not server.exhausted
              and (spec["max_summary_requests"] is None or server.summary_requests <= spec["max_summary_requests"]))
    return {
        "id": spec["id"], "part": "B", "note": spec["note"], "known_issue": spec["known_issue"],
        "verdict": "pass" if passed else "fail", "stop_reason": stop_reason, "compactions": kinds,
        "expected_kinds": spec["kinds"], "requests_checked": len(server.request_checks), "violations": violations,
        "fact_retention": retention, "summary_requests": server.summary_requests,
        "script_exhausted": server.exhausted, "error": error,
    }


def run_suite():
    rows = [run_a(spec) for spec in A_SCENARIOS] + [run_b(spec) for spec in B_SCENARIOS]
    summary = {
        "scenarios": len(rows),
        "passed": sum(row["verdict"] == "pass" for row in rows),
        "failures": [row["id"] for row in rows if row["verdict"] != "pass" and not row["known_issue"]],
        "known_issues_reproduced": [row["id"] for row in rows if row["verdict"] != "pass" and row["known_issue"]],
    }
    return {"schema_version": SCHEMA_VERSION, "summary": summary, "rows": rows}
