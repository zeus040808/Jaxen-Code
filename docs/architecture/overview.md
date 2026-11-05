# Jaxen Code 说明文档

`jaxen` 是本仓库的本地 coding agent：在完整的工程骨架（OpenAI 兼容模型接口、
checkpoint、trace、审批、记忆）之上，增量吸收 `learn-claude-code` 的设计。
模块职责和调用链见 [模块导览](module-map.md)，2026-10-30 的 harness 改动见
[harness-upgrades-20261030.md](harness-upgrades-20261030.md)。

## 运行方式

只支持 **OpenAI Chat Completions 兼容**的大模型接口，默认 DeepSeek `deepseek-flash`：

```powershell
python -m jaxen --approval ask
python -m jaxen --approval auto "分析项目结构并提出改进建议"
python -m jaxen --model deepseek-v4-pro
```

| provider | 用途 | 配置 |
|---|---|---|
| `deepseek`（默认） | DeepSeek 预设 | `JAXEN_DEEPSEEK_API_KEY`、`JAXEN_DEEPSEEK_API_BASE`、`JAXEN_DEEPSEEK_MODEL` |
| `openai` | 任意兼容端点（通义千问兼容模式、OpenAI 等） | `JAXEN_OPENAI_API_KEY`，加 `--base-url`/`JAXEN_OPENAI_API_BASE` 与 `--model`/`JAXEN_OPENAI_MODEL`；不在内置窗口表里的模型用 `--context-window` |
| `fake` | 仅限隔离评测的脚本化模型 | `JAXEN_ALLOW_EVAL_FAKE_PROVIDER=1` |

每个用户请求默认最多 50 次有效工具调用（`--max-steps` 调整）。缓存命中的重复读取不消耗额度；
达到上限后进入 `final-only`，要求模型基于已有证据回答。

交互运行中可以切换模型，Session、历史、记忆、任务和审批策略都保留；新 provider 初始化失败时旧
client 保持不变：

```text
jaxen> /model
jaxen> /model deepseek deepseek-v4-pro
jaxen> /model openai qwen-plus
```

客户端流式读取 SSE：终端实时显示请求开始、reasoning 进行中和请求完成，但不打印私有推理；
原生 tool_calls 收齐后才交给 `AgentLoop`，避免执行半截参数。

## 流式终端

终端会实时输出模型回合状态、工具名/参数/结果、危险操作审批提示，以及最终答案或阻塞原因。
模型返回 `<needs_user_input>` 时显示 `? Need your input`，交互模式下一轮提示符变成 `choice>`。

上下文如何组装、何时压缩见 [context-engineering.md](context-engineering.md)，
模块职责和调用链见 [模块导览](module-map.md)。

## 保留的工程能力

`jaxen` 保留这些核心结构：

- `agent.CodingAgent`
- `AgentLoop`（单次请求的控制循环）
- `ToolExecutor`
- `ToolContext`
- `WorkspaceContext`
- `ContextManager`
- `RunStore`
- `SessionStore`
- `RunState`
- `ProgressTracker`
- checkpoint / resume 检查
- 稳定 system 前缀 + 只追加会话记录 + 按 token 预算压缩的上下文
- layered memory
- provider clients
- shell env allowlist
- secret redaction
- run artifacts：
  - `.jaxen/runs/<run_id>/run_state.json`
  - `.jaxen/runs/<run_id>/trace.jsonl`
  - `.jaxen/runs/<run_id>/report.json`

也就是说，这不是删减版，而是 Jaxen-branded 增强版。

## 新增能力

### 1. 长任务待办（backlog，只在 full 工具集）

跨会话保留的待办项，带依赖、验收标准和证据：

- `backlog_add`、`backlog_list`、`backlog_claim`、`backlog_complete`、`backlog_fail`、`backlog_block`

保存在 `.jaxen/backlog/`。它和当前请求的进度账本互不依赖：待办没做完不会拦当前请求的交付。
当前请求内的计划用 `todo_write`。设计来源是 `learn-claude-code` S12 的任务系统，2026-11-05 起和短任务拆开。

### 2. 后台命令

`run_shell` 带 `{"background": true}` 时在后台跑：

```xml
<tool>{"name":"run_shell","args":{"command":"pytest -q","timeout":120,"background":true}}</tool>
```

只有显式设置才会进后台。完成结果在下一次模型决策前以 `<job_notification>` 注入历史；测试和编译命令默认前台运行，
当前运行能立即拿到退出码。来源是 S13 background tasks。

### 3. 定时提示（用户功能）

用户在 CLI 里安排"到点发给 agent 的一条指令"，5 段 cron 表达式（分 时 日 月 周）：

```text
/schedule 0 9 * * 1-5 跑一遍测试并汇报失败
/schedules
/unschedule sched_ab12cd
```

保存在 `.jaxen/schedules.json`。到点后 CLI 把这条指令当成用户消息调用 `agent.ask()`，由 agent 决定读文件、
跑工具还是回答。**模型看不到任何定时工具**：要不要定时是用户的决定。来源是 S14 cron scheduler，2026-11-05
从模型工具改为用户命令。

### 4. CLI 命令

```text
/backlog     查看长任务待办
/schedule    安排定时提示；/schedules 查看，/unschedule <id> 取消
/review      只读审查当前未提交的改动
```

### 5. 进度账本与交付门禁

当前目标的进度保存在会话的 `progress` 里（`ProgressTracker`）：目标和状态、跨运行的续跑记录、todo、
代码改了测没测、工具失败、已验证 / 未验证的结论、待回答的问题、最近的观察。

模型交最终答案前，门禁检查：未解决的可重试工具失败、未验证的结论、待回答的问题、还在跑的后台命令。
还能动手时提醒一次，再交就带着未解决项放行（`incomplete_delivery`）。

完整设计见 [进度账本与交付门禁](progress-and-delivery.md)，概念关系见 [概念与模块地图](module-map.md)。

## 工程能力清单

`jaxen` 保留的工程特性：

- OpenAI Chat Completions 兼容接口（DeepSeek 预设）
- checkpoint / resume
- prompt metadata
- run trace / report
- memory promotion
- workspace fingerprint
- ToolExecutor metadata
- repeated tool call 防护
- approval policy
- secret redaction

然后在工具层和运行层补上长任务待办、后台命令、定时提示。

## 测试

增强能力测试：

```powershell
pytest tests/test_extensions.py -q
```

完整测试：

```powershell
pytest -q
```

当前验证结果（2026-10-30）：

```text
206 passed, 4 skipped
```

## 简历表述建议

可以写：

> 基于 Jaxen 完整 agent runtime 实现增强版本地 coding agent，支持任意 OpenAI 兼容大模型、checkpoint/resume、run trace/report、workspace fingerprint、layered memory、ToolExecutor 安全审计等工程能力，并增量加入 learn-claude-code 风格的持久化任务系统、后台工具执行和 cron 定时唤醒机制；通过 pytest 覆盖工具协议、任务流、后台通知、定时调度和 run artifact。
