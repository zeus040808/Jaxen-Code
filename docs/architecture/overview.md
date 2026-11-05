# HH Agent Pro 说明文档

`hh_agent_pro` 是本仓库的本地 coding agent：在完整的工程骨架（OpenAI 兼容模型接口、
checkpoint、trace、审批、记忆）之上，增量吸收 `learn-claude-code` 的设计。
模块职责和调用链见 [模块导览](module-map.md)，2026-10-30 的 harness 改动见
[harness-upgrades-20261030.md](harness-upgrades-20261030.md)。

## 运行方式

只支持 **OpenAI Chat Completions 兼容**的大模型接口，默认 DeepSeek `deepseek-flash`：

```powershell
python -m hh_agent_pro --approval ask
python -m hh_agent_pro --approval auto "分析项目结构并提出改进建议"
python -m hh_agent_pro --model deepseek-v4-pro
```

| provider | 用途 | 配置 |
|---|---|---|
| `deepseek`（默认） | DeepSeek 预设 | `HH_DEEPSEEK_API_KEY`、`HH_DEEPSEEK_API_BASE`、`HH_DEEPSEEK_MODEL` |
| `openai` | 任意兼容端点（通义千问兼容模式、OpenAI 等） | `HH_OPENAI_API_KEY`，加 `--base-url`/`HH_OPENAI_API_BASE` 与 `--model`/`HH_OPENAI_MODEL`；不在内置窗口表里的模型用 `--context-window` |
| `fake` | 仅限隔离评测的脚本化模型 | `HH_ALLOW_EVAL_FAKE_PROVIDER=1` |

每个用户请求默认最多 50 次有效工具调用（`--max-steps` 调整）。缓存命中的重复读取不消耗额度；
达到上限后进入 `final-only`，要求模型基于已有证据回答。

交互运行中可以切换模型，Session、历史、记忆、任务和审批策略都保留；新 provider 初始化失败时旧
client 保持不变：

```text
hh> /model
hh> /model deepseek deepseek-v4-pro
hh> /model openai qwen-plus
```

客户端流式读取 SSE：终端实时显示请求开始、reasoning 进行中和请求完成，但不打印私有推理；
原生 tool_calls 收齐后才交给 `AgentLoop`，避免执行半截参数。

## 流式终端

终端会实时输出模型回合状态、工具名/参数/结果、危险操作审批提示，以及最终答案或阻塞原因。
模型返回 `<needs_user_input>` 时显示 `? Need your input`，交互模式下一轮提示符变成 `choice>`。

上下文如何组装、何时压缩见 [context-engineering.md](context-engineering.md)，
模块职责和调用链见 [模块导览](module-map.md)。

## 保留的工程能力

`hh_agent_pro` 保留这些核心结构：

- `agent.CodingAgent`
- `AgentLoop`（单次请求的控制循环）
- `ToolExecutor`
- `ToolContext`
- `WorkspaceContext`
- `ContextManager`
- `RunStore`
- `SessionStore`
- `RunState`
- `TaskState`
- checkpoint / resume 检查
- 稳定 system 前缀 + 只追加会话记录 + 按 token 预算压缩的上下文
- layered memory
- provider clients
- shell env allowlist
- secret redaction
- run artifacts：
  - `.hh/runs/<run_id>/run_state.json`
  - `.hh/runs/<run_id>/trace.jsonl`
  - `.hh/runs/<run_id>/report.json`

也就是说，这不是删减版，而是 HH-branded 增强版。

## 新增能力

### 1. 持久化项目任务

新增工具：

- `create_task`
- `list_tasks`
- `claim_task`
- `complete_task`
- `fail_task`
- `block_task`
- `record_evidence`

任务文件保存到：

```text
.hh/tasks/
```

这部分来自 `learn-claude-code` S12 / `hh_agent.task_system` 的设计。

### 2. 后台 shell 任务

`run_shell` 新增参数：

```json
{"background": true}
```

示例：

```xml
<tool>{"name":"run_shell","args":{"command":"pytest -q","timeout":120,"background":true}}</tool>
```

只有显式设置 `background: true` 才会进入后台。完成结果会在同一个 Agent Loop 的下一次模型决策前以 `<task_notification>` 注入 history；测试和编译命令默认前台运行，以便当前 Run 立即拿到退出码。

这部分来自 S13 background tasks。

### 3. Cron 定时唤醒

新增工具：

- `schedule_cron`
- `list_crons`
- `cancel_cron`

定时任务保存到：

```text
.hh/scheduled_tasks.json
```

到点后不会直接执行 shell，而是把 prompt 转成一条新的用户消息：

```text
[Scheduled] ...
```

然后仍由 Agent 决定是否读文件、运行工具或回答。

这部分来自 S14 cron scheduler。

### 4. CLI 增强

新增 REPL 命令：

```text
/tasks   查看持久化项目任务
/crons   查看定时任务
```

启动 REPL 后会自动启动 cron scheduler，并在 Agent 空闲时交付定时任务。

### 5. TaskState 与交付门

当前用户任务的语义进度保存在 Session 的 `task_state` 中，包括：

- 当前目标和状态
- 跨 Run 的续跑记录
- 当前任务关联的子任务 ID
- 工具证据和失败记录
- 已验证/未验证结论
- 开放问题和下一步

ProjectTask 支持 `pending`、`in_progress`、`completed`、`failed`、`blocked`、`cancelled`，并可保存验收条件、证据和失败原因。

模型返回 `<final>` 前，Runtime 会拒绝以下提前交付：

- 当前任务仍有 `pending` 或 `in_progress` 子任务
- 子任务处于 `failed` 或 `blocked`
- 存在未验证结论或开放问题
- 存在未解决的可恢复工具失败
- 仍有后台任务运行

完整设计见 [`HH_TASK_STATE_AND_DELIVERY_GATE_ZH.md`](task-state-and-delivery.md)。

## 工程能力清单

`hh_agent_pro` 保留的工程特性：

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

然后在工具层和运行层补上任务、后台、定时能力。

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

> 基于 HH 完整 agent runtime 实现增强版本地 coding agent，支持任意 OpenAI 兼容大模型、checkpoint/resume、run trace/report、workspace fingerprint、layered memory、ToolExecutor 安全审计等工程能力，并增量加入 learn-claude-code 风格的持久化任务系统、后台工具执行和 cron 定时唤醒机制；通过 pytest 覆盖工具协议、任务流、后台通知、定时调度和 run artifact。
