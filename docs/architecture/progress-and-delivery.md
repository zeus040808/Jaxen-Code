# 进度账本与交付门禁

`progress.py` 的 `ProgressTracker` 记录**一个目标**的进度：用户提出一件事，可能要多次运行才做完（中途澄清、
被打断后继续），账本在这几次运行之间保留。它回答"目标是什么、做到哪了、还有什么没解决"，交付门禁和每轮的
末尾提醒都读它。

和它容易混的几个概念（详见 [概念与模块地图](module-map.md)）：

| 概念 | 区别 |
|---|---|
| `RunState`（`run_state.py`） | 只管一次运行：次数、步数、停止原因 |
| 长任务待办 `Backlog`（`services/backlog.py`） | 跨会话的长任务清单；**和账本互不依赖，待办没做完不拦当前请求的交付** |
| 源码证据 `SourceEvidence`（`source_evidence.py`） | 读过哪些文件、哪几行、内容变没变；账本里的 `observations` 只是最近工具结果的摘要 |

## 1. 账本里有什么

```json
{
  "goal_id": "goal_ab12cd34ef",
  "goal": "修复 parser 的空格处理（保留前 1000 字符）",
  "status": "running",
  "run_ids": ["run_..."],
  "continuation_requests": ["The target is production."],
  "todos": [{"content": "补测试", "status": "in_progress"}],
  "verification_pending": true,
  "verification": {"edit_revision": 2, "changed_paths": ["parser.py"], "checks": [...]},
  "failures": [{"tool": "run_shell", "error_code": "tool_failed", "resolved": false, "retryable": true}],
  "claims": [{"claim": "tests pass", "evidence": "pytest: 8 passed", "verified": true}],
  "open_questions": [],
  "observations": [{"kind": "tool", "tool": "read_file", "summary": "..."}],
  "next_action": ""
}
```

| 字段 | 谁写 | 用途 |
|---|---|---|
| `goal`、`goal_id` | 运行开始时 `begin()` | 新目标重置账本；同一目标的后续运行（澄清后继续）追加 `run_ids` 和 `continuation_requests` |
| `todos` | 模型调用 `todo_write`（整体替换） | 当前目标的计划，每轮显示在提醒里；只是提示，不拦交付 |
| `verification` | 工具执行后自动 | 改了代码（`.py` 等）后有没有跑过测试；只提醒（"Code verification: pending test"），不拦交付 |
| `failures` | 工具执行后自动 | 失败的工具调用；之后同类检查成功、或只读工具出错后有成功调用，就标为已解决 |
| `claims` | 模型调用 `record_claim`（full 工具集） | 结论 + 证据；未验证的结论拦交付 |
| `open_questions` | 模型请求澄清时 | 用户回答后清空 |
| `observations` | 自动 | 最近的工具结果、后台命令通知、交付记录的摘要 |

## 2. 末尾提醒

每轮请求最后一条消息里的 `<system-reminder>` 由 `render()` 生成，以 `Goal progress:` 开头：目标、状态、
todo、代码验证情况、最后一次检查、未解决的失败、未验证的结论、待回答的问题、最近的观察。它放在请求末尾，
每轮变化也不影响前面的前缀缓存。紧接着是 `Run progress:`（本次运行的决策数、工具步数）。

## 3. 交付门禁

模型给出最终答案时，`delivery_blockers()` 检查：

1. 还在跑的后台命令；
2. 未解决且可重试的工具失败；
3. 未验证的结论；
4. 待回答的问题。

规则（`agent_loop.py`）：

- 模型**还能动手**（工具没被禁用、步数没用完）时提醒一次（`completion_deferred`），把未解决项告诉它；
- 再交一次就放行，未解决项照实写进答案，停止原因记为 `incomplete_delivery`（`completion_forced`）；
- 动不了手时不驳回，直接带着说明交付（避免"解决需要工具、工具又不给"的死锁）。

改了代码没跑测试**不拦**：正则认不全所有合法的验证方式（复现脚本、仓库自定义命令），硬拦会误报，还会逼模型
自己写测试文件来过关。门禁之后，改过源码时还有一次审查子 agent（`review.py`）。

## 4. 恢复

账本存在会话 JSON 里（`session["progress"]`）。`CodingAgent.from_session()` 重建它；检查点（`checkpoint.py`）
里带着账本的摘要（目标、已完成的 todo、当前阻塞、下一步、已验证的结论），恢复时放进提醒。

## 5. 演进

2026-11-05 之前，这个类叫 `TaskState`，并且和"项目任务"（现在的长任务待办）绑在一起：当前请求里建的项目任务
没做完会拦交付，账本的提醒里也列出这些子任务。长任务和短任务混在一起，名字也都叫 task，容易误以为是同一个东西。
现在拆开：账本只管当前目标，长任务待办只管跨会话的事；原来的 `record_evidence` 工具改名为 `record_claim`，归入账本。

测试：`tests/test_core_capacity.py`（验证追踪、失败解决规则）、`tests/test_delivery_gate_deadlock.py`（门禁不死锁）、
`tests/test_extensions.py`（结论拦交付、待办不拦交付、账本跨运行保留、待办跨会话保留）、`tests/test_todo_tool.py`。
