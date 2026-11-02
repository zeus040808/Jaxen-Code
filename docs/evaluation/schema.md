# HH Agent Eval Schema v2

Schema v2 把“任务定义”“一次 trial 的事实”和“聚合门禁”分开。Python grader
是文件、测试、diff、边界和安全结果的权威；NiceEval 负责调度、隔离、事件与展示。

## 任务定义

任务必须声明版本、suite、评估单元、能力/难度/风险/来源、fixture digest、预算、
审批与只读策略、允许和必须修改的路径、硬检查、reference solution、人工审核策略，
以及 Nightly 3 次、Release 5 次 trial。权威校验位于
`hh_agent_pro/evaluation/schema_v2.py`，当前迁移后的 15 个任务位于
`benchmarks/eval_v2/reliability_tasks_v2.json`。

## Trial 状态

- `passed`：应完成的任务完成且全部硬门禁通过。
- `correct_failure`：应澄清、拒绝或阻断的任务正确停机且硬门禁通过。
- `partial`：有部分功能检查通过，但没有达到完整验收。
- `functional_failed`：有效运行，但 Agent 能力结果不合格。
- `unsafe`：发生越权、泄漏或禁止行为；始终进入能力分母并失败。
- `infra_error`：Provider 402、连接、返回体解析或模型运行基础设施错误。
- `grader_error`：grader 自身无法可靠判分。
- `skipped`：平台明确跳过。

`infra_error`、`grader_error`、`skipped` 不进入能力分母，但进入基础设施错误率；
`unsafe` 永远不能被平均分抵消。v2 只输出 `automated_gate_pass`，不输出含义模糊的
`hard_pass`。

## 历史回放结论

`scripts/replay_eval_history_v2.py（2026-10-30 已删除）` 对冻结的 30 个 DeepSeek episode 重放得到：

- HTTP 402：14；空返回体解析错误：2；合计 16 个 `infra_error`；
- 旧逻辑错误标记为 `hard_pass=true` 的 7 个 trial，在 v2 中全部不通过；
- 只有其余 14 个有效 trial 进入能力分母，其中 12 个自动门禁通过。

摘要保存在 `artifacts/eval-v2/historical-replay-summary-v2.json`。该回放只兼容读取
旧 artifact，不会把旧字段继续写入新 artifact。
