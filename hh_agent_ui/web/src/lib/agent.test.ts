// 事件 → 界面块 的回归测试：node --test（Node 22+ 直接运行 TypeScript，不需要测试框架）
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyEvent, buildPrompt, emptyUsage, toolSummary, totalUsage, type Thread } from "./agent.ts";

const newThread = (): Thread => ({
  id: "t", title: "t", cwd: "/repo", sessionId: null, blocks: [], usageBase: emptyUsage(), usage: null, updated: 0,
});

test("工具调用：开始是 running，结束按 tool_status 变成 ok / failed", () => {
  const t = newThread();
  applyEvent(t, { kind: "event", type: "operation.started", operationId: "op_1", name: "read_file", input: { path: "a.py" } });
  assert.equal(t.blocks[0].type === "tool" && t.blocks[0].status, "running");
  applyEvent(t, { kind: "event", type: "operation.finished", operationId: "op_1", name: "read_file", output: "x = 1", metadata: { tool_status: "ok" } });
  assert.deepEqual(t.blocks[0], { type: "tool", id: "op_1", name: "read_file", input: { path: "a.py" }, status: "ok", output: "x = 1" });
});

test("审批请求比工具结束先到时，被拦下的调用显示为等待审批而不是失败", () => {
  const t = newThread();
  applyEvent(t, { kind: "event", type: "operation.started", operationId: "op_1", name: "run_shell", input: { command: "pytest" } });
  applyEvent(t, { kind: "event", type: "input.requested", inputId: "input_approval_1", name: "run_shell", question: "Approve run_shell?", arguments: { command: "pytest" } });
  applyEvent(t, { kind: "event", type: "operation.finished", operationId: "op_1", name: "run_shell", output: "approval required before run_shell", metadata: { tool_status: "rejected" } });
  assert.equal(t.blocks[0].type === "tool" && t.blocks[0].status, "waiting");
  assert.equal(t.blocks[1].type, "approval");
});

test("真正的失败（后面没有审批）仍显示为失败", () => {
  const t = newThread();
  applyEvent(t, { kind: "event", type: "operation.started", operationId: "op_1", name: "patch_file", input: { path: "a.py" } });
  applyEvent(t, { kind: "event", type: "operation.finished", operationId: "op_1", name: "patch_file", output: "old_text not found", metadata: { tool_status: "error" } });
  assert.equal(t.blocks[0].type === "tool" && t.blocks[0].status, "failed");
});

test("只显示模型的思考摘要，内部计数和请求完成状态不显示", () => {
  const t = newThread();
  applyEvent(t, { kind: "event", type: "thinking", text: "Thinking - decision 1/150 | tools 0/50", source: "status" });
  applyEvent(t, { kind: "event", type: "thinking", text: "DeepSeek turn completed", source: "provider_status" });
  applyEvent(t, { kind: "event", type: "thinking", text: "先跑测试看看", source: "reasoning_summary" });
  assert.deepEqual(t.blocks, [{ type: "thinking", text: "先跑测试看看" }]);
});

test("turn 记下 sessionId；token 用量按对话累计", () => {
  const t = newThread();
  t.usageBase = { inputTokens: 100, outputTokens: 10, requests: 1 };
  applyEvent(t, { kind: "event", type: "usage", usage: { inputTokens: 50, outputTokens: 5, requests: 1 } });
  applyEvent(t, { kind: "turn", status: "completed", sessionId: "s1", usage: { inputTokens: 60, outputTokens: 6, requests: 2 } });
  assert.equal(t.sessionId, "s1");
  assert.deepEqual(totalUsage(t), { inputTokens: 160, outputTokens: 16, requests: 3 });
});

test("进程失败且没有任何回答时，把 finalAnswer 作为错误显示", () => {
  const t = newThread();
  applyEvent(t, { kind: "turn", status: "failed", finalAnswer: "API key is missing" });
  assert.deepEqual(t.blocks, [{ type: "error", text: "API key is missing" }]);
});

test("工具摘要优先显示命令、搜索模式、路径", () => {
  assert.equal(toolSummary({ command: "pytest -q" }), "pytest -q");
  assert.equal(toolSummary({ pattern: "def foo", path: "src" }), "def foo  in src");
  assert.equal(toolSummary({ paths: ["a.py", "b.py"] }), "a.py, b.py");
});

test("选定文件和上传文件分别写进指令：前者是修改范围，后者只作参考", () => {
  assert.equal(buildPrompt("修 bug", []), "修 bug");
  const prompt = buildPrompt("修 bug", [
    { path: "calc.py", source: "workspace" },
    { path: ".hh/uploads/spec.md", source: "upload" },
  ]);
  assert.equal(prompt,
    "修 bug\n\n[选定文件] 只修改下面这些文件；需要理解上下文时可以阅读其他文件。\n- calc.py" +
    "\n\n[用户上传的参考文件] 先阅读它们，不要修改。\n- .hh/uploads/spec.md");
});
