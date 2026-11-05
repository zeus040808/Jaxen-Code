"""错误恢复模块的故障注入评测（假模型，零成本，可复现）。

做法：每个场景在一个新的临时目录里放一个小仓库（add() 有 bug + 单元测试），新建一个 agent，
接上**真实的** OpenAIChatClient，但把它底下的 urllib 换成假服务器：按脚本返回模型回复，并在指定位置
注入故障（HTTP 错误、超时、断流、空响应、截断、坏 JSON……）。agent 循环、工具、门禁、trace 全是真代码。

判分：隐藏检查（在工作区里跑单元测试）+ 工作区比对（只允许改 calc.py）+ 停止原因 + trace 断言。
结果分四类：recovered 救回 / stopped 正确停止 / misfire 误触发或停错 / silent_failure 报成功但没做对。

隔离：临时目录（含 .hh 状态）、每个场景新建 agent、urllib 补丁用完即恢复、运行期间清掉 API key。
"""

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path

from ..agent import CodingAgent
from ..providers import OpenAIChatClient
from ..providers import openai_chat
from ..run_store import RunStore
from ..session_store import SessionStore
from ..workspace import WorkspaceContext

SCHEMA_VERSION = "fault-injection-v1"
TEST_COMMAND = "python -m unittest -q"
FIXTURE = {
    "calc.py": "def add(a, b):\n    return a - b\n",
    "test_calc.py": "import unittest\nfrom calc import add\n\n\nclass AddTest(unittest.TestCase):\n"
                    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
}
TASK = "add() in calc.py returns the wrong result. Fix it and run the tests."

# ---- 脚本积木：模型的一轮回复，或者一次故障 ----


def tool(name, **args):
    return {"tool": name, "args": args}


def final(text="Fixed add() in calc.py; the unit tests pass."):
    return {"text": f"<final>{text}</final>"}


def text(raw, finish="stop"):
    return {"text": raw, "finish": finish}


def http(code, retry_after=None):
    return {"fault": "http", "code": code, "retry_after": retry_after}


TIMEOUT = {"fault": "timeout"}
BROKEN_STREAM = {"fault": "broken_stream"}
EMPTY = {"text": "", "finish": "stop"}             # 只有思考，没有正文也没有工具调用
EMPTY_TRUNCATED = {"text": "", "finish": "length"}  # 思考用光输出额度，正文为空
BAD_NATIVE_ARGS = {"bad_native_args": True}         # 原生 tool_calls 的 arguments 不是合法 JSON
DSML = text('<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="read_file">\n'
            '<｜｜DSML｜｜parameter name="path">calc.py</｜｜DSML｜｜parameter>\n</｜｜DSML｜｜invoke>\n</｜｜DSML｜｜tool_calls>')

READ = tool("read_file", path="calc.py")
# 修复后的文件故意和原来不一样大：Python 按"修改时间（秒）+ 大小"判断 .pyc 是否过期，脚本一秒内就改完再测，
# 大小相同会沿用旧字节码，改对了测试照样失败（shell 只继承白名单环境变量，PYTHONDONTWRITEBYTECODE 传不进去）
PATCH = tool("patch_file", path="calc.py", old_text="return a - b", new_text="return a + b  # fixed")
WRONG_PATCH = tool("patch_file", path="calc.py", old_text="return a - b", new_text="return a * b")
RUN_TESTS = tool("run_shell", command=TEST_COMMAND)
FIX = [READ, PATCH, RUN_TESTS, final()]  # 正确解法
RETRY = "request failed ("  # provider 重试时写进 trace 的提示（"retry" 本身会和工具元数据里的 retryable 撞上）


def scenario(sid, layer, script, outcome, stop_reason="final_answer_returned", *, control=False,
             trace=(), no_trace=(), known_issue="", fixture_extra=None, note=""):
    return {"id": sid, "layer": layer, "script": script, "control": control, "note": note,
            "fixture_extra": fixture_extra or {}, "known_issue": known_issue,
            "expect": {"outcome": outcome, "stop_reason": stop_reason, "trace": list(trace), "no_trace": list(no_trace)}}


SCENARIOS = [
    # 网络层：真实 provider 的重试代码在跑
    scenario("baseline", "network", FIX, "recovered", control=True, no_trace=[RETRY], note="无故障基线"),
    scenario("net_503_once", "network", [http(503), *FIX], "recovered", trace=[RETRY]),
    scenario("net_429_retry_after", "network", [http(429, "2"), *FIX], "recovered", trace=[RETRY]),
    scenario("net_timeout", "network", [READ, TIMEOUT, PATCH, RUN_TESTS, final()], "recovered", trace=[RETRY]),
    scenario("net_broken_stream", "network", [READ, PATCH, BROKEN_STREAM, RUN_TESTS, final()], "recovered", trace=[RETRY]),
    scenario("net_503_twice_behind_proxy", "network", [READ, http(503), http(503), PATCH, RUN_TESTS, final()],
             "recovered", trace=[RETRY], note="假服务器拒绝复用的 Request 对象（模拟代理把第二次请求变成 CONNECT host:80）"),
    scenario("net_503_exhausted", "network", [http(503)] * openai_chat.MAX_ATTEMPTS, "stopped", "model_error"),
    scenario("net_401", "network", [http(401)], "stopped", "model_error", no_trace=[RETRY]),
    # 模型输出层
    scenario("empty_once", "model_output", [READ, EMPTY, PATCH, RUN_TESTS, final()], "recovered", trace=["resending"]),
    scenario("empty_persistent", "model_output", [EMPTY] * (openai_chat.EMPTY_RESPONSE_RETRIES + 1), "stopped", "model_error"),
    scenario("truncated_with_text", "model_output",
             [READ, PATCH, RUN_TESTS, text("<final>Fixed add() in calc.py", "length"), text("; the unit tests pass.</final>")],
             "recovered", trace=["model_output_truncated", "output_recovery_started"]),
    scenario("truncated_empty", "model_output", [READ, EMPTY_TRUNCATED, PATCH, RUN_TESTS, final()],
             "recovered", trace=["output_budget_raised"], note="fix7：正文为空的截断提高输出上限重发"),
    scenario("truncated_twice", "model_output",
             [READ, PATCH, RUN_TESTS, text("<final>Fixed", "length"), text(" add()", "length")], "stopped", "output_truncated"),
    scenario("bad_text_tool_json", "model_output", [READ, text('<tool>{"name": "patch_file", "args": {</tool>'), *FIX[1:]],
             "recovered", trace=["model_output_rejected"]),
    scenario("bad_native_tool_args", "model_output", [READ, BAD_NATIVE_ARGS, *FIX[1:]], "recovered",
             trace=["model_output_rejected"],
             note="原生 tool_calls 的参数 JSON 坏了：像文本格式一样让模型重来（2026-11-05 发现后修复：原来整轮以 model_error 结束）"),
    scenario("dsml_once", "model_output", [READ, DSML, *FIX[1:]], "recovered", trace=["internal_tool_protocol_rejected"]),
    scenario("dsml_twice", "model_output", [READ, DSML, DSML], "stopped", "model_protocol_error"),
    # 工具层
    scenario("repeat_failing_test_no_progress", "tool", [READ, RUN_TESTS, RUN_TESTS, PATCH, RUN_TESTS, final()],
             "stopped", "repeated_failed_call", trace=["repeated_failed_call"]),
    scenario("rerun_failed_test_after_edit", "tool", [READ, RUN_TESTS, PATCH, RUN_TESTS, final()], "recovered",
             control=True, no_trace=["repeated_failed_call"], note="fix6：改完代码后重跑之前失败的测试不是死循环"),
    scenario("search_missing_path_then_continue", "tool",
             [tool("search", pattern="def add", path="no_such_dir"), *FIX], "recovered", control=True,
             no_trace=["completion_deferred"], note="fix5：只读搜索出错之后有成功调用，不应卡交付"),
    # 交付层
    scenario("deliver_without_test", "delivery", [READ, PATCH, final()], "recovered", trace=["Code verification: pending test"],
             no_trace=["completion_deferred"],
             note="按设计不拦：改了代码没跑测试只在交付前那一轮提醒模型（正则认不全所有合法的验证方式）"),
    scenario("deliver_after_failed_test", "delivery", [READ, WRONG_PATCH, RUN_TESTS, final(), final()],
             "stopped", "incomplete_delivery", trace=["completion_deferred"]),
    scenario("test_runner_missing_then_script_check", "delivery",
             [READ, PATCH, tool("run_shell", command="python -m pytest -q"),
              tool("run_shell", command="python check_add.py"), final(), final()],
             "recovered", control=True, no_trace=["completion_deferred"],
             fixture_extra={"pytest/__init__.py": "", "check_add.py": "from calc import add\nassert add(2, 3) == 5\nprint('ok')\n"},
             known_issue="测试命令因环境问题失败（这里模拟没装 pytest）后用脚本验证通过，门禁仍会拦（SWE-bench astropy-8707、django-16662）"),
]


# ---- 假服务器：替换 urllib.request.urlopen ----


class _Stream:
    def __init__(self, lines, break_midway=False):
        self.lines, self.break_midway = lines, break_midway

    def __enter__(self):
        def gen():
            for index, line in enumerate(self.lines):
                if self.break_midway and index == 1:
                    raise ConnectionResetError("connection reset by peer")
                yield line
        return gen()

    def __exit__(self, *exc):
        return False


def _sse(delta, finish):
    events = [{"choices": [{"delta": {"reasoning_content": "thinking", **delta}, "finish_reason": finish}]},
              {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10}}]
    return [("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode() for event in events] + [b"data: [DONE]\n"]


class FakeServer:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.max_tokens = []
        self.exhausted = False
        self._seen_requests = set()

    def urlopen(self, request, timeout=None):
        if id(request) in self._seen_requests:  # 复用同一个 Request 对象：代理下会退化成 CONNECT host:80
            raise urllib.error.URLError("proxy rejected CONNECT host:80 (reused Request object)")
        self._seen_requests.add(id(request))
        self.max_tokens.append(json.loads(request.data)["max_tokens"])
        if not self.script:
            self.exhausted = True
            raise urllib.error.HTTPError(request.full_url, 400, "fake script exhausted", {}, io.BytesIO(b"exhausted"))
        step = self.script.pop(0)
        self.calls += 1
        fault = step.get("fault")
        if fault == "http":
            headers = {"Retry-After": step["retry_after"]} if step.get("retry_after") else {}
            raise urllib.error.HTTPError(request.full_url, step["code"], "fault", headers, io.BytesIO(b'{"error":"fault"}'))
        if fault == "timeout":
            raise socket.timeout("read timed out")
        if fault == "broken_stream":
            return _Stream(_sse({"content": "partial"}, None), break_midway=True)
        if step.get("bad_native_args"):
            call = {"index": 0, "id": f"call_{self.calls}", "function": {"name": "patch_file", "arguments": '{"path": "calc.py",'}}
            return _Stream(_sse({"tool_calls": [call]}, "tool_calls"))
        if "tool" in step:
            call = {"index": 0, "id": f"call_{self.calls}",
                    "function": {"name": step["tool"], "arguments": json.dumps(step["args"])}}
            return _Stream(_sse({"tool_calls": [call]}, "tool_calls"))
        delta = {"content": step["text"]} if step["text"] else {}
        return _Stream(_sse(delta, step.get("finish", "stop")))


# ---- 运行一个场景 ----


@contextlib.contextmanager
def _isolated_process_env():
    """运行期间：清掉 API key 和代理变量（假服务器之外不可能联网），让 shell 里的 python 指向当前解释器。"""
    saved = dict(os.environ)
    for name in list(os.environ):
        if name.endswith(("_API_KEY", "_TOKEN")) or name.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
            os.environ.pop(name)
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + saved.get("PATH", "")
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".hh" not in p.relative_to(root).parts and "__pycache__" not in p.parts}


def _hidden_check(root):
    result = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_calc"], cwd=root,
                            capture_output=True, text=True, timeout=60)
    return result.returncode == 0


def _trace_text(agent):
    """trace + transcript：注入给模型的提示原文只在 transcript 里。"""
    run_state = agent.current_run_state
    if not run_state:
        return ""
    paths = (agent.run_store.trace_path(run_state), agent.run_store.transcript_path(run_state))
    return "\n".join(path.read_text(encoding="utf-8") for path in paths if path.exists())


def run_scenario(spec):
    workdir = Path(tempfile.mkdtemp(prefix="hh-fault-"))
    root = workdir / "repo"
    root.mkdir()
    try:
        for rel, content in {**FIXTURE, **spec["fixture_extra"]}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(content, encoding="utf-8")
        before = _snapshot(root)
        server = FakeServer(spec["script"])
        client = OpenAIChatClient("deepseek-flash", "https://fake.invalid", "fake-key", 0.0, 5, label="Fake")
        client.sleep = lambda seconds: None
        agent = CodingAgent(model_client=client, workspace=WorkspaceContext.build(root, repo_root_override=root),
                            session_store=SessionStore(root / ".hh" / "sessions"), run_store=RunStore(root / ".hh" / "runs"),
                            approval_policy="auto", max_steps=10, max_new_tokens=1024, toolset="core")
        original = openai_chat.urllib.request.urlopen
        openai_chat.urllib.request.urlopen = server.urlopen
        error = ""
        try:
            with _isolated_process_env():
                agent.ask(TASK)
        except Exception as exc:  # model_error 等会以异常结束 ask()，停止原因已写进 run_state
            error = f"{exc.__class__.__name__}: {exc}"[:300]
        finally:
            openai_chat.urllib.request.urlopen = original
        return _grade(spec, agent, server, root, before, error)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _grade(spec, agent, server, root, before, error):
    run_state = agent.current_run_state
    stop_reason = run_state.stop_reason if run_state else "no_run"
    trace = _trace_text(agent)
    after = _snapshot(root)
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    task_correct = _hidden_check(root) and set(changed) <= {"calc.py"}
    claimed_success = stop_reason == "final_answer_returned"
    if claimed_success and not task_correct:
        outcome = "silent_failure"
    elif claimed_success:
        outcome = "recovered"
    else:
        outcome = "stopped"
    expect = spec["expect"]
    markers = {marker: marker in trace for marker in expect["trace"] + expect["no_trace"]}
    passed = (outcome == expect["outcome"] and stop_reason == expect["stop_reason"]
              and all(markers[m] for m in expect["trace"]) and not any(markers[m] for m in expect["no_trace"])
              and not server.exhausted)
    verdict = "pass" if passed else ("silent_failure" if outcome == "silent_failure" else "misfire")
    return {
        "id": spec["id"], "layer": spec["layer"], "control": spec["control"], "note": spec["note"],
        "known_issue": spec["known_issue"], "verdict": verdict, "outcome": outcome, "stop_reason": stop_reason,
        "expected": expect, "task_correct": task_correct, "changed_files": changed, "trace_markers": markers,
        "model_requests": server.calls, "script_exhausted": server.exhausted,
        "max_tokens_requested": sorted(set(server.max_tokens)), "error": error,
    }


def run_suite(scenarios=SCENARIOS):
    rows = [run_scenario(spec) for spec in scenarios]
    regular = [row for row in rows if not row["known_issue"]]
    summary = {
        "scenarios": len(rows),
        "passed": sum(row["verdict"] == "pass" for row in rows),
        "silent_failures": sum(row["outcome"] == "silent_failure" for row in rows),
        "misfires": [row["id"] for row in rows if row["verdict"] == "misfire"],
        "control_misfires": [row["id"] for row in rows if row["control"] and row["verdict"] != "pass"],
        "known_issues_reproduced": [row["id"] for row in rows if row["known_issue"] and row["verdict"] != "pass"],
        "regular_failures": [row["id"] for row in regular if row["verdict"] != "pass"],
        "by_layer": {},
    }
    for row in rows:
        layer = summary["by_layer"].setdefault(row["layer"], {"scenarios": 0, "passed": 0})
        layer["scenarios"] += 1
        layer["passed"] += row["verdict"] == "pass"
    return {"schema_version": SCHEMA_VERSION, "summary": summary, "rows": rows}
