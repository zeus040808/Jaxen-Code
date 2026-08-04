# HH Coder Agent 说明文档

`hh_coder_agent` 是在当前仓库中新建的独立 coding agent，不覆盖原有 `hh_agent`。

它的定位是：把 `HH` 的稳定工程骨架和 `learn-claude-code` 的教学机制合并成一个适合简历展示的本地 agent harness。

## 运行方式

```powershell
python -m hh_coder_agent --approval ask
```

一次性任务：

```powershell
python -m hh_coder_agent --approval auto "分析这个项目并给出修改建议"
```

常用环境变量：

```text
HH_AGENT_API_KEY     优先使用；没有时回退 DEEPSEEK_API_KEY
HH_AGENT_BASE_URL    优先使用；没有时回退 BASE_URL
HH_AGENT_MODEL       优先使用；没有时回退 LLM_MODEL_ID
```

## 架构来源

来自 HH 的设计：

- 独立运行态 `HHCoderAgent`
- workspace 沙箱
- XML 工具协议
- session 持久化
- run artifact：`task_state.json`、`trace.jsonl`、`report.json`
- approval policy：`ask`、`auto`、`never`
- 可测试的 `ScriptedModel`

来自 learn-claude-code 的设计：

- agent loop
- 工具调用协议
- todo_write
- 安全 hook/权限思想
- 上下文裁剪和 prompt metadata
- 任务系统
- 后台任务
- cron 定时任务

## 目录结构

```text
hh_coder_agent/
  agent.py          主运行态和 agent loop
  cli.py            命令行入口
  context.py        prompt 构造和上下文预算
  tools.py          工具注册与实现
  security.py       沙箱、审批、脱敏
  stores.py         session/run 持久化
  state.py          run state 数据结构
  workspace.py      工作区扫描和路径约束
  task_system.py    持久化项目任务
  background.py     后台任务
  cron.py           定时任务
  models.py         OpenAI 兼容模型与测试模型
```

运行产物写在：

```text
.hh_coder/
  sessions/
  runs/
  tasks/
  scheduled_tasks.json
```

## 模型协议

模型必须返回以下两类之一：

```xml
<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>
```

或：

```xml
<final>最终回答</final>
```

写多行文件时也支持 HH 风格 XML：

```xml
<tool name="write_file" path="notes.txt"><content>
hello
</content></tool>
```

## 工具能力

- `list_files`
- `read_file`
- `write_file`
- `patch_file`
- `search`
- `run_shell`
- `todo_write`
- `create_task`
- `list_tasks`
- `claim_task`
- `complete_task`
- `schedule_cron`
- `list_crons`
- `cancel_cron`

## 简历表述建议

可以写成：

> 实现本地 coding agent harness，支持 XML 工具协议、仓库上下文构建、沙箱文件工具、shell 审批策略、会话持久化、run trace/report、长期任务系统、后台任务与 cron 定时唤醒；参考 HH 的运行态拆分和 learn-claude-code 的 agent loop/工具/上下文/任务系统设计，并补充 pytest 测试覆盖。

## 测试

```powershell
pytest tests/test_hh_coder_agent.py -q
```

完整项目测试：

```powershell
pytest -q
```

