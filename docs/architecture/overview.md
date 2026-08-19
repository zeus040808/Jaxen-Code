# HH Agent Pro 说明文档

`hh_agent_pro` 是一个完整工程基底上的增强版 HH coding agent。

它和 `hh_coder_agent` 的定位不同：

- `hh_coder_agent`：学习友好的精简融合版，代码更短，方便讲清楚。
- `hh_agent_pro`：产品化增强版，完整保留上游工程骨架，再增量加入 `learn-claude-code` / `hh_agent` 的优点。

## 运行方式

HH Agent Pro 默认使用 DeepSeek `deepseek-v4-pro`，同时支持显式切换到
Codex CLI、Ollama、OpenAI-compatible 和 Anthropic-compatible 后端：

```powershell
python -m hh_agent_pro --approval ask
```

每个用户请求默认最多执行 12 次有效工具调用。需要为某次启动临时调整时使用：

```powershell
python -m hh_agent_pro --max-steps 20
```

缓存命中的重复读取不会重复消耗有效工具额度；达到上限后 Agent 会进入
`final-only`，要求模型基于已有证据回答，而不是继续执行工具。

一次性任务：

```powershell
python -m hh_agent_pro --approval auto "分析项目结构并提出改进建议"
```

如果要切换到 Codex，再确认本机 Codex CLI 可用：

```powershell
codex --version
codex login status
```

默认不传参数时选择 DeepSeek V4 Pro。需要临时覆盖模型时可使用：

```powershell
python -m hh_agent_pro --model <model-name>
```

Codex 子进程采用 `exec --json --ephemeral --sandbox read-only`。Codex 只负责返回下一步决策；文件写入、Shell、审批、Session、记忆与工具审计仍由 HH runtime 执行。

启动时选择 provider：

```powershell
python -m hh_agent_pro --provider codex
python -m hh_agent_pro --provider deepseek
python -m hh_agent_pro --provider ollama --model qwen3.5:4b
python -m hh_agent_pro --provider openai --model gpt-5.4
python -m hh_agent_pro --provider anthropic --model claude-sonnet-4-6
```

交互运行中可以查看或切换模型，不需要退出 Agent：

```text
hh> /model
hh> /model deepseek
hh> /model codex
hh> /model deepseek deepseek-reasoner
```

运行中切换只替换 `agent.model_client`。当前 Session、history、working memory、
项目任务、工具和审批策略都保留；如果新 provider 缺少密钥或初始化失败，旧 client
保持不变。

DeepSeek 从项目 `.env` 读取独立配置：

```dotenv
HH_DEEPSEEK_API_KEY=your-key
HH_DEEPSEEK_API_BASE=https://api.deepseek.com
HH_DEEPSEEK_MODEL=deepseek-v4-pro
```

DeepSeek 使用 Chat Completions SSE。终端实时显示请求开始、reasoning 进行中和请求完成，
但不会打印服务端的私有逐字推理；完整的 HH `<tool>/<final>` 协议文本收齐后才交给
`AgentLoop` 解析，避免半截 JSON 或半截 XML 标签被执行。

## 流式终端

终端会实时输出：

- 当前模型回合与网络重连状态；
- Codex JSONL 提供的 reasoning summary；
- HH 工具名、参数、状态与裁剪后的结果；
- 危险操作审批提示；
- 最终答案或阻塞原因。

这里的 `Reasoning summary` 是可审计的简短推理摘要，不是模型隐藏思维链。隐藏思维链不会打印。

当模型返回 `<needs_user_input>` 时，终端会显示 `? Need your input`，交互模式下一轮提示符变成 `choice>`；一次性模式若终端可交互，也会停下来等待补充。

针对 Windows 多文件交互、工具选择和上下文裁剪的完整修复说明见
[`HH_AGENT_PRO_INTERACTION_TOOLCHAIN_REPAIR_ZH.md`](HH_AGENT_PRO_INTERACTION_TOOLCHAIN_REPAIR_ZH.md)。

新增两个只读基础工具：

- `runtime_info`：返回脱敏的 provider、model、平台、shell 和工作区信息；
- `read_files`：单次读取最多 8 个工作区文件，供多文件比较和总结。

核心规则、完整工具目录和 runtime identity 现在属于不可裁剪区；只有 Git 状态和
项目文档快照允许压缩。DeepSeek 使用原生 function tool schema，Codex 仍保持
`read-only + ephemeral` 边界。

## 保留的工程能力

`hh_agent_pro` 保留这些核心结构：

- `runtime.HH`
- `AgentLoop`
- `ToolExecutor`
- `ToolContext`
- `WorkspaceContext`
- `ContextManager`
- `RunStore`
- `SessionStore`
- `RunState`
- `TaskState`
- checkpoint / resume 检查
- prompt prefix / prompt cache metadata
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

完整设计见 [`HH_TASK_STATE_AND_DELIVERY_GATE_ZH.md`](HH_TASK_STATE_AND_DELIVERY_GATE_ZH.md)。

## 为什么这版比精简版更强

精简版 `hh_coder_agent` 只实现了一个小型可讲解 harness。

`hh_agent_pro` 保留了完整工程特性：

- 多 provider
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

所以它更适合作为“产品化 coding agent 项目”放进简历。

## 测试

增强能力测试：

```powershell
pytest tests/test_hh_agent_pro_extensions.py -q
```

完整测试：

```powershell
pytest -q
```

当前验证结果：

```text
161 passed
```

## 简历表述建议

可以写：

> 基于 HH 完整 agent runtime 实现增强版本地 coding agent，保留多 provider、checkpoint/resume、run trace/report、workspace fingerprint、layered memory、ToolExecutor 安全审计等工程能力，并增量加入 learn-claude-code 风格的持久化任务系统、后台工具执行和 cron 定时唤醒机制；通过 pytest 覆盖工具协议、任务流、后台通知、定时调度和 run artifact。
