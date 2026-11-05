# 模块导览：从一次请求看代码

先记住三个不同的生命周期：

| 对象 | 存活范围 | 谁负责 |
|---|---|---|
| 程序 | 从启动到退出 | `cli.py` |
| Agent 与 Session | 可跨多条用户请求 | `agent.py` 的 `CodingAgent` |
| 一次请求 | 一次 `ask()`，内部可多轮调用模型 | `agent_loop.py` 的 `AgentLoop` |

统一使用 `from hh_agent_pro.agent import CodingAgent`（旧的 `runtime.py` 与别名 `HH` 已于 2026-10-30 删除）。

## 一次请求的调用链

```text
__main__.py → cli.py → CodingAgent.ask() → AgentLoop.run()
                                      ├─ ContextManager.build() → 模型客户端
                                      ├─ ToolExecutor.execute() → 工具实现
                                      └─ SessionStore / RunStore → 落盘
```

Loop 每轮重新组装 prompt，让模型选择工具或回答。工具结果写入 Session，下一轮才能被模型看到。
Loop 在完成、等待用户、阻塞、超限等分支结束；Agent 继续保留 Session，供下一条请求使用。

## 核心模块

| 模块 | 一句话职责 |
|---|---|
| `__main__.py` | `python -m hh_agent_pro` 的最薄入口。 |
| `__init__.py` | 包的公开导入接口。 |
| `cli.py` | 解析启动参数、创建 Agent、读取终端输入；不实现模型决策循环。 |
| `agent.py` | 装配模型、工具、上下文与状态，提供 `ask()` 和供 Loop 调用的能力。 |
| `agent_loop.py` | 控制单次请求的模型调用、工具反馈、重试与停止条件。 |
| `run_state.py` | 一次 `ask()` 的状态快照，例如尝试次数和停止原因。 |
| `task_state.py` | 当前任务的语义进度与交付门禁，可跨一次请求延续。 |
| `session_store.py` | 保存和加载完整 Session JSON。 |
| `run_store.py` | 保存单次运行的状态、trace 和报告。 |
| `checkpoint.py` | 生成恢复检查点并检查工作区是否仍与检查点一致。 |
| `streaming.py` | 定义 Agent 向界面发送事件的接口。 |
| `terminal_ui.py` | 将事件显示在交互终端。 |
| `markdown_console.py` | 将最终 Markdown 文本渲染成终端可读格式。 |
| `jsonl_protocol.py` | 为评测和自动化提供非交互 JSONL 接口。 |

## 模型、上下文与工作区

| 模块 | 一句话职责 |
|---|---|
| `providers/base.py` | `ModelRequest`（一次模型调用的全部输入）与已知模型的窗口表。 |
| `providers/openai_chat.py` | OpenAI Chat Completions 兼容客户端：流式 SSE、原生 tool_calls、usage/缓存命中。 |
| `providers/scripted.py` | 测试与 CI 评测用的脚本化确定性模型。 |
| `config.py` | 读取项目配置及环境变量。 |
| `workspace.py` | 构建工作区快照并提供文件信息辅助函数。 |
| `prompt_prefix.py` | 构建 system 的稳定部分：规则、工具策略、项目指令、会话开始时的环境快照。 |
| `context_manager.py` | 组装每轮请求（稳定 system + 只追加会话记录 + 末尾提醒），按 token 预算在接近上限时压缩。 |
| `evidence.py` | 记录本任务读取过的源码证据，避免仅靠被裁剪的聊天历史。 |

## 工具与长期服务

| 模块 | 一句话职责 |
|---|---|
| `tools.py` | 定义工具目录、参数规则和具体工具函数。 |
| `tool_context.py` | 给工具函数传入受限依赖，避免直接访问整个 Agent。 |
| `tool_executor.py` | 统一校验、审批、执行工具并归一化结果。 |
| `security.py` | 路径、环境和输出中的安全检查与敏感信息脱敏。 |
| `services/background_tasks.py` | 管理耗时 Shell 命令的后台执行与通知。 |
| `services/cron_scheduler.py` | 排队未来要交给 Agent 的定时请求。 |
| `services/project_tasks.py` | 持久化项目任务及其状态。 |
| `services/__init__.py` | 长期服务子包的接口说明。 |

## 记忆与技能

| 模块 | 一句话职责 |
|---|---|
| `memory/working.py` | 保存当前任务的短期工作状态。 |
| `memory/curated_store.py` | 保存可编辑、带来源的长期事实。 |
| `memory/session_archive.py` | 建立跨 Session 的可检索历史索引。 |
| `memory/learner.py` | 从明确的用户陈述提取候选长期事实。 |
| `memory/models.py` | 定义长期记忆的数据结构。 |
| `memory/policy.py` | 规定哪些事实可写入或检索。 |
| `memory/__init__.py` | 记忆子包的公开接口。 |
| `skills/store.py` | 存储并按需读取可复用流程。 |
| `skills/learner.py` | 从已验证成功的流程中生成 Skill。 |
| `skills/__init__.py` | Skill 子包的公开接口。 |

## 离线评测

`evaluation/` 不参与在线决策循环；它创建测试 Agent、检查产物并汇总指标。

| 模块 | 一句话职责 |
|---|---|
| `evaluation/evaluator.py` | 运行固定任务并收集结果。 |
| `evaluation/metrics.py` | 计算汇总指标和对照报告。 |
| `evaluation/schema_v2.py` | 定义企业评测的数据结构和分类。 |
| `evaluation/enterprise_report_v2.py` | 汇总企业评测多次试验。 |
| `evaluation/reliability.py` | 评估真实运行的结果和工具轨迹。 |
| `evaluation/memory_v2.py` | 对照评估记忆机制。 |
| `evaluation/__init__.py` | 评测子包的接口说明。 |
