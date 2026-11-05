# 审查子 agent（2026-11-03）

## 结论

新增一个**由 harness 强制触发**的只读审查子 agent：主 agent 改过代码、准备交付时，派一个上下文全新的子 agent
按清单审查需求和 diff；有问题就把意见交回主 agent 继续改，最多一轮。同一套审查也给用户自己用（`/review`、界面上的"AI 审查"按钮）。

## 为什么

| 依据 | 数据 |
|---|---|
| 失败类型 | 隔离环境和新题评测里，"在症状处兜底"（xarray-6938、sphinx-7462）、"漏改第二个文件"（django-13212、pylint-4551）、"需求没覆盖全"（django-16631）占失败的一半以上，主 agent 自己复查发现不了 |
| 现有子 agent 没人用 | 唯一的子 agent `delegate`（只读调查）由模型自己决定是否调用，全部 SWE-bench 记录（约 150 次运行）里调用次数为 0 |

所以审查不能做成"可选工具"，要在固定时机由 harness 触发。审查者只看到需求和 diff、看不到主 agent 的推理，相当于换一双眼睛。

## 设计

| 部分 | 做法 | 位置 |
|---|---|---|
| 审查清单 | 根因还是症状、谁还受影响（调用方/包装/前后同类代码）、需求是否覆盖全（含后续状态）、范围是否合适、有没有与新原则矛盾的旧代码；只报会让改动出错或不完整的问题 | `hh_agent_pro/skills/builtin/code-review/SKILL.md`（skill 格式，唯一来源） |
| 只读子 agent | 和 `delegate` 共用 `readonly_child()`：只能读、搜、看结构，不能改文件和执行命令；审查最多 12 步 | `agent.py` |
| 审查流程 | 取 `git diff HEAD` + 未跟踪新文件（跳过 `.hh/`、`__pycache__`、二进制）→ 拼审查任务 → 解析 `VERDICT: approve / issues` 和问题列表；格式不对按通过处理，不卡交付 | `review.py` |
| 触发时机 | 交付检查通过之后、拼装最终回答之前；条件：开关打开、本次运行还没审查过、工作区有改动、还有工具步数 | `agent_loop.py` |
| 有问题时 | 把意见作为一条运行时提示交回主 agent；主 agent 可以不采纳，但要在答案里说明理由；第二次交付不再审查 | `review.feedback_notice` |
| 审查需求的原文 | 用运行开始时的完整用户消息（`task_state.goal` 被截断到 1000 字，不够） | `agent_loop.py` |
| token 用量 | 子 agent 的用量经 `UsageForwardingSink` 转给父 agent，算进本轮；工具卡片和回答不转发，不混进父 agent 的界面。顺带修好了 `delegate` 用量不计入的老问题 | `streaming.py` |
| 界面显示 | 审查过程显示为一张 `code_review` 工具卡片（结论和问题列表） | — |
| 用户命令 | `/review [说明]`：审查当前未提交改动，只出报告不改代码；CLI、JSONL、界面按钮都走 `agent.ask()` 里同一个分支 | `agent.py`、`jsonl_protocol.py`、`App.tsx` |

## 触发策略（三档）

| 档位 | 什么时候审查 | 默认用在 |
|---|---|---|
| `off` | 从不（`/review` 仍可手动用） | SWE-bench 评测脚本（和之前的结果保持可比）、`--provider fake`、Python API |
| `auto` | 改了**非测试的源代码**才审查；只改文档、配置、测试时跳过 | CLI 对真实模型 |
| `always` | 有任何改动就审查 | 想要最严格时 |

不按改动行数判断：xarray-6938 只改了 1 行，恰恰是"在症状处兜底"、最该审的那种。源代码按 `task_state.CODE_SUFFIXES` 判断，
测试文件按路径判断（`tests/`、`test_*.py`、`*_test.py`、`*.spec.ts` 等）。默认档位最终要靠"审查开/关"评测数据来定。

## 同步执行与时间上限

子 agent（调查和审查）都是**同步**的：父 agent 停下来等它跑完。为了不把总时长撑破，子 agent 的时长上限是父 agent 本次运行
**剩下的时间**（`agent.remaining_time_ms()`，AgentLoop 开始时设 `run_deadline`），而不是完整时长。步数上限照旧：调查 20 步、审查 12 步，
到上限就停下，带着状态和不完整的结论返回；审查没给出结论按通过处理。

没有做后台异步子 agent：主循环一次只做一件事、模型也不主动派子 agent，异步体现不出好处，还要处理模型客户端的线程安全。
项目里已有一套后台任务完成通知（`agent_loop.py`，后台 shell 命令用，作为一条 user 消息插入对话，和 Claude Code 的
`<task-notification>` 是同一思路），以后要做后台子 agent 可以复用。

## 返回格式（信封）

参考 Claude Code 的 `<task-notification>`，子 agent 的结果装进固定格式的信封返回（`subagent.py`）：

```xml
<subagent-result>
<kind>delegate</kind>
<status>completed</status>
<stop-reason>final_answer_returned</stop-reason>
<summary>前 300 个字符</summary>
<result>结果（最多 4000 字符）</result>
<output-file>.hh/subagents/delegate-1a2b3c4d.md</output-file>   <!-- 只有结果过长时才有 -->
<files>- 读过的文件: 摘要（最多 12 个）</files>
<usage><input-tokens/><output-tokens/><requests/><tool-uses/><duration-ms/></usage>
</subagent-result>
```

- 结果太长时完整内容写进 `.hh/subagents/`，信封里只放截断版和路径，父 agent 需要细节再 `read_file`：上下文保持精简。
- 用量由 `UsageForwardingSink` 累计（同时转给父 agent，算进本轮总数），工具次数取子 agent 的 `tool_steps`，耗时单独计。
- 审查子 agent 也走同一个 `run_child()`：结论交回主 agent 时用 `feedback_notice`（只在有问题时），用量写进 trace 的 `review_completed`。
- 以前 `delegate` 返回 `delegate_result:` 加一段 JSON，没有用量和耗时；依赖旧格式的 2 个测试已改为检查信封字段。

## 实测（deepseek-flash，示例仓库）

| 场景 | 结果 |
|---|---|
| 修 `average` 的 bug | 交付前出现 `code_review` 卡片，结论 approve，正常交付；8 次请求已含审查 |
| `/review`（第一次） | 报告里把 `__pycache__/*.pyc` 当成改动——仓库没有 `.gitignore`，编译产物是未跟踪文件。已修：跳过 `__pycache__`、`.pyc` 和二进制文件 |
| `/review`（修复后） | 按清单逐条检查（根因在 `average` 本身、唯一调用方是测试文件……），结论 approve |

## 真实任务观察（2026-11-03）

新中等题里 2 道失败题用 `--review auto` 重跑：审查都运行了、都给 approve，成本占本题输入 token 的 16%–27%。
xarray-6938 两种修法都合理、隐藏测试锁定了其中一种，审查没拦住。详见 [重跑结果](../results/2026-11-03-review-rerun.md)。
子 agent 的 trace 和主 agent 在同一目录，`run_started` 里的 `depth` 用来区分（分析脚本只取 depth=0）。

## 代价与限制

- 每次改代码多一次审查，预计多用 20%～40% token（推测，待评测确认）。
- 审查者可能误报，所以没有否决权、只审一轮。
- 审批续跑那一轮，审查拿到的是截断后的 `goal`（审批结果里不带原文）；日常很少遇到。
- **效果还没在评测里证明**：计划用第二批新中等题和新难题做"审查开/关"对比。

## 测试

2026-11-03 增补：三档触发（7 个参数化用例）、`auto` 模式只改文档时不审查、信封带用量且长结果写文件、子 agent 只拿到剩余时间。

`tests/test_review.py`（12 个）：结论解析、diff 收集（含新文件，跳过 `.hh/`、编译产物、二进制）、有问题交回主 agent 且只审一轮、
通过时直接交付、没改代码或关掉开关时不审查、`/review` 只出报告不改代码、JSONL 下 `/review` 是完成的一轮并带回答、
子 agent 只转发用量、CLI 默认值随 provider 变化。全量 `pytest` 304 passed；确定性回归均 100%。
