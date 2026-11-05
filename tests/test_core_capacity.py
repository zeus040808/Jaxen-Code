"""Small end-to-end checks for parallel reads and honest task completion."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from hh_agent_pro.providers import ModelRequest, OpenAIChatClient
from hh_agent_pro.agent import CodingAgent
from hh_agent_pro.session_store import SessionStore
from hh_agent_pro.task_state import TaskState, _check_kind
from hh_agent_pro.workspace import WorkspaceContext


class ScriptedNative:
    requires_reasoning_content_for_tool_calls = False
    provider = "test"
    model = "scripted"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.last_completion_metadata = {}
        self.last_native_assistant_message = {}
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        answer = self.outputs.pop(0)
        self.last_native_assistant_message = (
            {"content": "", "reasoning_content": "", "tool_calls": [
                {"id": "read_a", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "read_b", "name": "read_file", "args": {"path": "b.txt"}},
            ]} if answer.startswith("<tool_batch>") else {}
        )
        return answer


class CoreCapacityTests(unittest.TestCase):
    def test_check_command_recognizes_assertions_without_shell_masking(self):
        self.assertEqual(_check_kind('python -c "from acme.text import parse_bool; assert parse_bool(True)"'), "test")
        self.assertEqual(_check_kind("pytest -q || true"), "")
        self.assertEqual(_check_kind("pytest --collect-only"), "")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "a.txt").write_text("alpha\n", encoding="utf-8")
        (self.root / "b.txt").write_text("beta\n", encoding="utf-8")

    def agent(self, model=None, max_steps=4):
        return CodingAgent(
            model_client=model or ScriptedNative(["<final>done</final>"]),
            workspace=WorkspaceContext.build(self.root, repo_root_override=self.root),
            session_store=SessionStore(self.root / ".hh" / "sessions"),
            approval_policy="auto", max_steps=max_steps,
        )

    def test_read_batch_runs_concurrently_and_preserves_order(self):
        agent = self.agent()
        barrier = threading.Barrier(2)

        def runner(args):
            barrier.wait(timeout=2)
            return args["path"]

        agent.tools["read_file"]["run"] = runner
        results = agent.tool_executor.execute_read_batch([
            {"name": "read_file", "args": {"path": "a.txt"}},
            {"name": "read_file", "args": {"path": "b.txt"}},
        ])
        self.assertEqual([item.content for item in results], ["a.txt", "b.txt"])

    def test_batch_results_are_replayed_as_one_native_turn(self):
        batch = ('<tool_batch>['
                 '{"id":"read_a","name":"read_file","args":{"path":"a.txt"}},'
                 '{"id":"read_b","name":"read_file","args":{"path":"b.txt"}}'
                 ']</tool_batch>')
        model = ScriptedNative([batch, "<final>both read</final>"])
        agent = self.agent(model)
        self.assertEqual(agent.ask("read both files"), "both read")
        messages = model.requests[1].messages
        assistant = next(item for item in messages if item.get("tool_calls"))
        self.assertEqual([call["id"] for call in assistant["tool_calls"]], ["read_a", "read_b"])
        self.assertEqual([item["tool_call_id"] for item in messages if item["role"] == "tool"],
                         ["read_a", "read_b"])
        self.assertEqual(agent.current_run_state.tool_steps, 2)

    def test_provider_keeps_every_native_call(self):
        class Response:
            def __enter__(self):
                return iter([
                    b'data: {"choices":[{"delta":{"tool_calls":['
                    b'{"index":0,"id":"read_a","function":{"name":"read_file","arguments":"{\\"path\\":\\"a.txt\\"}"}},'
                    b'{"index":1,"id":"read_b","function":{"name":"read_file","arguments":"{\\"path\\":\\"b.txt\\"}"}}'
                    b']},"finish_reason":"tool_calls"}]}\n',
                    b'data: [DONE]\n',
                ])

            def __exit__(self, exc_type, exc, traceback):
                return False

        client = OpenAIChatClient(
            "deepseek-chat", "https://api.deepseek.com", "key", 0.2, 5
        )
        with patch("hh_agent_pro.providers.openai_chat.urllib.request.urlopen", return_value=Response()):
            raw = client.complete(ModelRequest("rules", [{"role": "user", "content": "read both"}], 100, tools=[{"type": "function"}]))
        self.assertEqual(CodingAgent.parse(raw)[0], "tool_batch")
        self.assertEqual(client.last_completion_metadata["native_tool_calls_returned"], 2)
        self.assertEqual(len(client.last_native_assistant_message["tool_calls"]), 2)

    def test_unrelated_read_does_not_resolve_failed_patch(self):
        agent = self.agent()
        failed = agent.execute_tool("patch_file", {
            "path": "a.txt", "old_text": "absent", "new_text": "new",
        })
        self.assertNotEqual(failed.metadata["tool_status"], "ok")
        agent.execute_tool("read_file", {"path": "b.txt"})
        self.assertTrue(any("patch_file" in item for item in agent.delivery_blockers()))
        agent.execute_tool("patch_file", {
            "path": "a.txt", "old_text": "alpha", "new_text": "new",
        })
        self.assertFalse(any("patch_file" in item for item in agent.delivery_blockers()))

    def test_unknown_tool_name_does_not_block_delivery(self):
        # 模型把 run_shell 写成 shell：提示它换工具，但这次失败不该一直挂在交付阻塞里。
        agent = self.agent()
        result = agent.execute_tool("shell", {"command": "ls"})
        self.assertEqual(result.metadata["tool_error_code"], "unknown_tool")
        self.assertIn("unknown tool", result.content)
        self.assertEqual(agent.delivery_blockers(), [])

    def test_reread_after_edit_and_revert_returns_real_content(self):
        # 改了又撤回（内容恢复原样）之后再读：是正常工作，要返回真实内容，也不算"重复读取"。
        # SWE-bench django-12325（Opus）：撤回后回头看代码，只拿到"内容没变"的摘要，再读一次就被禁用了全部工具。
        agent = self.agent()
        agent.execute_tool("read_file", {"path": "a.txt"})
        agent.execute_tool("patch_file", {"path": "a.txt", "old_text": "alpha", "new_text": "ALPHA"})
        agent.execute_tool("patch_file", {"path": "a.txt", "old_text": "ALPHA", "new_text": "alpha"})
        reread = agent.execute_tool("read_file", {"path": "a.txt"})
        self.assertNotIn("cached observation", reread.content)
        self.assertIn("alpha", reread.content)
        self.assertFalse(reread.metadata.get("cache_hit", False))

    def test_repeat_count_resets_after_real_progress(self):
        # 读 → 跑别的工具 → 再读：中间有进展，不累计成"连续重复"。
        agent = self.agent()
        agent.execute_tool("read_file", {"path": "a.txt"})
        first = agent.execute_tool("read_file", {"path": "a.txt"})
        self.assertEqual(first.metadata["cache_hit_count"], 1)
        agent.execute_tool("read_file", {"path": "b.txt"})
        again = agent.execute_tool("read_file", {"path": "a.txt"})
        self.assertEqual(again.metadata["cache_hit_count"], 1)

    def test_rejected_arguments_do_not_block_delivery(self):
        # 读了一个不存在的路径（参数校验没过，什么都没执行），之后换对路径继续：
        # 同一个错误路径永远不会成功，这次失败不该一直挂到交付（SWE-bench django-11265 误报 incomplete_delivery）。
        agent = self.agent()
        result = agent.execute_tool("read_file", {"path": "no/such/file.py"})
        self.assertEqual(result.metadata["tool_error_code"], "invalid_arguments")
        self.assertTrue(result.metadata["retryable"])  # 仍告诉模型"改好参数可以重试"
        agent.execute_tool("read_file", {"path": "b.txt"})
        self.assertEqual(agent.delivery_blockers(), [])

    def test_failed_read_only_search_does_not_block_delivery(self):
        # search 一个不存在的文件是运行时出错（tool_failed），不是参数校验失败；之后有调用成功同样算绕过去了。
        # SWE-bench sphinx-8638：猜错文件名搜了一次，交付被拦、强制交付成 incomplete_delivery。
        agent = self.agent()
        result = agent.execute_tool("search", {"pattern": "alpha", "path": "no/such/module.py"})
        self.assertNotEqual(result.metadata["tool_status"], "ok")
        self.assertTrue(result.metadata["read_only"])
        self.assertTrue(agent.delivery_blockers())  # 失败之后一次成功都没有，仍然拦下
        agent.execute_tool("read_file", {"path": "b.txt"})
        self.assertEqual(agent.delivery_blockers(), [])

    def test_failed_shell_check_still_blocks_delivery(self):
        # 会改东西的 shell 命令（测试）失败，不能被随便一次成功的读取"绕过去"。
        agent = self.agent()
        agent.tools["run_shell"]["run"] = lambda args: "exit_code: 1\nFAILED\n"
        result = agent.execute_tool("run_shell", {"command": "python -m pytest -q", "timeout": 20})
        self.assertEqual(result.metadata["tool_error_code"], "tool_failed")
        self.assertFalse(result.metadata["read_only"])
        agent.execute_tool("read_file", {"path": "b.txt"})
        self.assertTrue(agent.delivery_blockers())

    def test_rerunning_failed_command_after_edit_is_not_a_loop(self):
        # 复现脚本失败 → 改代码 → 再跑同一个脚本：是"改完验证"，不该被判成重复失败停掉整个运行。
        # SWE-bench django-12754：补丁其实已经对了，运行却以 repeated_failed_call 结束。
        agent = self.agent()
        agent.tools["run_shell"]["run"] = lambda args: "exit_code: 1\nTraceback\n"
        command = {"command": "python repro.py", "timeout": 20}
        first = agent.execute_tool("run_shell", command)
        self.assertEqual(first.metadata["tool_error_code"], "tool_failed")
        agent.execute_tool("read_file", {"path": "a.txt"})
        agent.execute_tool("patch_file", {"path": "a.txt", "old_text": "alpha", "new_text": "ALPHA"})
        again = agent.execute_tool("run_shell", command)
        self.assertEqual(again.metadata["tool_error_code"], "tool_failed")
        self.assertFalse(again.metadata.get("stop_run", False))

    def test_rerunning_failed_command_without_progress_still_stops(self):
        # 中间什么都没改就原样重跑同一条失败命令：仍然是死循环，照旧停下。
        agent = self.agent()
        agent.tools["run_shell"]["run"] = lambda args: "exit_code: 1\nTraceback\n"
        command = {"command": "python repro.py", "timeout": 20}
        agent.execute_tool("run_shell", command)
        agent.execute_tool("read_file", {"path": "a.txt"})
        again = agent.execute_tool("run_shell", command)
        self.assertEqual(again.metadata["tool_error_code"], "repeated_failed_call")
        self.assertTrue(again.metadata["stop_run"])

    def test_unresolved_failure_is_delivered_as_incomplete(self):
        model = ScriptedNative([
            '<tool>{"name":"patch_file","args":{"path":"a.txt","old_text":"absent","new_text":"new"}}</tool>',
            "<final>done</final>",
        ])
        agent = self.agent(model, max_steps=1)
        answer = agent.ask("fix a.txt")
        self.assertIn("Unresolved at delivery", answer)
        self.assertEqual(agent.current_run_state.stop_reason, "incomplete_delivery")
        self.assertEqual(agent.task_state.to_dict()["status"], "paused")

    def test_code_edit_needs_post_edit_check(self):
        (self.root / "sample.py").write_text("value = 1\n", encoding="utf-8")
        agent = self.agent()
        agent.execute_tool("patch_file", {
            "path": "sample.py", "old_text": "value = 1", "new_text": "value = 2",
        })
        self.assertTrue(agent.task_state.to_dict()["verification_pending"])
        # 只记录、只提示，不再阻止交付（正则识别不了所有合法的验证方式）。
        self.assertFalse(any("post-edit test" in item for item in agent.delivery_blockers()))
        agent.tools["run_shell"]["run"] = lambda args: (
            "exit_code: 1\n" if args["command"].startswith("pytest -q") else "exit_code: 0\n"
        )
        agent.execute_tool("run_shell", {"command": "echo test", "timeout": 20})
        self.assertTrue(agent.task_state.to_dict()["verification_pending"])
        agent.execute_tool("run_shell", {"command": "pytest --collect-only", "timeout": 20})
        self.assertTrue(agent.task_state.to_dict()["verification_pending"])
        agent.execute_tool("run_shell", {"command": "python3 -m compileall -q sample.py", "background": True, "timeout": 20})
        self.assertTrue(agent.task_state.to_dict()["verification_pending"])
        agent.execute_tool("run_shell", {"command": "python3 -m compileall -q sample.py", "timeout": 20})
        self.assertTrue(agent.task_state.to_dict()["verification_pending"])
        agent.execute_tool("run_shell", {"command": "pytest -q tests/test_sample.py", "timeout": 20})
        self.assertTrue(agent.task_state.to_dict()["verification_pending"])
        agent.execute_tool("patch_file", {
            "path": "sample.py", "old_text": "value = 2", "new_text": "value = 3",
        })
        agent.execute_tool("run_shell", {"command": "python3 -m pytest -q tests/test_sample.py", "timeout": 20})
        self.assertFalse(agent.task_state.to_dict()["verification_pending"])
        verification = agent.task_state.to_dict()["verification"]
        self.assertEqual(verification["edit_revision"], 2)
        self.assertEqual([check["passed"] for check in verification["checks"]], [True, False, True])
        self.assertEqual(verification["checks"][-1]["edit_revision"], 2)
        self.assertEqual(verification["checks"][-1]["changed_paths"], ["sample.py"])
        agent.execute_tool("patch_file", {
            "path": "sample.py", "old_text": "value = 3", "new_text": "value = 4",
        })
        self.assertTrue(agent.task_state.to_dict()["verification_pending"])

    def test_edit_at_step_limit_gets_verification_step(self):
        (self.root / "sample.py").write_text("value = 1\n", encoding="utf-8")
        for index in range(7):
            (self.root / f"read_{index}.txt").write_text(str(index), encoding="utf-8")
        model = ScriptedNative([
            *[
                f'<tool>{{"name":"read_file","args":{{"path":"read_{index}.txt"}}}}</tool>'
                for index in range(7)
            ],
            '<tool>{"name":"patch_file","args":{"path":"sample.py",'
            '"old_text":"value = 1","new_text":"value = 2"}}</tool>',
            '<tool>{"name":"run_shell","args":{"command":"python3 -m pytest -q tests/test_sample.py",'
            '"timeout":20}}</tool>',
            "<final>Changed and checked.</final>",
        ])
        agent = self.agent(model, max_steps=8)
        agent.tools["run_shell"]["run"] = lambda args: "exit_code: 0\n"
        self.assertEqual(agent.ask("fix sample.py"), "Changed and checked.")
        self.assertEqual(agent.current_run_state.tool_steps, 9)
        self.assertEqual(agent.task_state.to_dict()["status"], "completed")

    def test_unchecked_code_edit_is_delivered_with_a_verification_notice(self):
        (self.root / "sample.py").write_text("value = 1\n", encoding="utf-8")
        model = ScriptedNative([
            '<tool>{"name":"patch_file","args":{"path":"sample.py",'
            '"old_text":"value = 1","new_text":"value = 2"}}</tool>',
            "<final>Done.</final>",
        ])
        agent = self.agent(model, max_steps=1)

        answer = agent.ask("fix sample.py")

        self.assertEqual(answer, "Done.")
        self.assertEqual(agent.current_run_state.stop_reason, "final_answer_returned")
        self.assertEqual(agent.task_state.to_dict()["status"], "completed")
        # 模型交付前那一轮看得到"还没测"的提示。
        self.assertIn("Code verification: pending test", model.requests[-1].messages[-1]["content"])

    def test_old_pending_verification_can_be_resumed(self):
        agent = self.agent()
        old_state = agent.task_state.to_dict()
        old_state["verification_pending"] = True
        old_state.pop("verification")
        restored = TaskState(old_state, agent.project_tasks)
        self.assertEqual(restored.to_dict()["verification"]["edit_revision"], 1)
        self.assertTrue(restored.to_dict()["verification_pending"])

    def test_failed_test_is_not_resolved_by_unrelated_shell_success(self):
        agent = self.agent()
        agent.tools["run_shell"]["run"] = lambda args: (
            "exit_code: 1\n" if args["command"] == "pytest -q" else "exit_code: 0\n"
        )
        agent.execute_tool("run_shell", {"command": "pytest -q", "timeout": 20})
        agent.execute_tool("run_shell", {"command": "echo ready", "timeout": 20})
        self.assertTrue(any("run_shell" in item for item in agent.delivery_blockers()))
        agent.execute_tool("run_shell", {"command": "python3 -m pytest -q", "timeout": 20})
        self.assertFalse(any("run_shell" in item for item in agent.delivery_blockers()))

    def test_delegate_returns_status_and_cannot_mutate_project_tasks(self):
        agent = self.agent(ScriptedNative(["<final>child answer</final>"]))
        result = agent.spawn_delegate({"task": "inspect a.txt", "max_steps": 2})
        # 信封格式（subagent.py）：状态、结果、用量都在固定标签里
        self.assertTrue(result.startswith("<subagent-result>"))
        self.assertIn("<status>completed</status>", result)
        self.assertIn("<result>child answer</result>", result)
        self.assertIn("<tool-uses>0</tool-uses>", result)
        child = CodingAgent(
            model_client=ScriptedNative(["<final>done</final>"]),
            workspace=WorkspaceContext.build(self.root, repo_root_override=self.root),
            session_store=SessionStore(self.root / ".hh" / "sessions"),
            approval_policy="never", depth=1, max_depth=1, read_only=True,
        )
        self.assertNotIn("create_task", child.tools)

    def test_full_goal_stays_in_the_request_without_a_history_window(self):
        agent = self.agent()
        goal = "修复跨文件接口兼容性：" + "详细约束" * 350 + "必须保留旧接口"
        agent.record({"role": "user", "content": goal})
        for index in range(12):
            agent.record({"role": "assistant", "content": f"step {index}"})

        request, _ = agent.context_manager.prepare(goal)

        # 没有"只取最近 N 条"的窗口：最早的请求原文仍在会话记录里。
        self.assertIn("必须保留旧接口", request.text())

if __name__ == "__main__":
    unittest.main()
