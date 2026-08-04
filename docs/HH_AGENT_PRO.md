# HH Agent Pro 说明文档

`hh_agent_pro` 是一个完整工程基底上的增强版 HH coding agent。

它和 `hh_coder_agent` 的定位不同：

- `hh_coder_agent`：学习友好的精简融合版，代码更短，方便讲清楚。
- `hh_agent_pro`：产品化增强版，完整保留上游工程骨架，再增量加入 `learn-claude-code` / `hh_agent` 的优点。

## 运行方式

```powershell
python -m hh_agent_pro --approval ask
```

一次性任务：

```powershell
python -m hh_agent_pro --approval auto "分析项目结构并提出改进建议"
```

它继承并统一了多 provider 能力：

```powershell
python -m hh_agent_pro --provider deepseek
python -m hh_agent_pro --provider openai
python -m hh_agent_pro --provider anthropic
python -m hh_agent_pro --provider ollama
```

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

慢命令会立刻返回后台任务 ID，完成后在下一轮 `ask()` 开始时以 `<task_notification>` 注入 history。

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
94 passed
```

## 简历表述建议

可以写：

> 基于 HH 完整 agent runtime 实现增强版本地 coding agent，保留多 provider、checkpoint/resume、run trace/report、workspace fingerprint、layered memory、ToolExecutor 安全审计等工程能力，并增量加入 learn-claude-code 风格的持久化任务系统、后台工具执行和 cron 定时唤醒机制；通过 pytest 覆盖工具协议、任务流、后台通知、定时调度和 run artifact。
