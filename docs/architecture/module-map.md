# 概念与模块地图

读代码前先看这一页：每个概念叫什么、活多久、在哪个文件、谁驱动、和谁有关系。
2026-11-05 做过一次命名与边界整理（项目改名 Jaxen、拆开长短任务、定时提示改为用户功能），对照表见文末。

## 一、三条生命周期线

| 范围 | 是什么 | 谁负责 |
|---|---|---|
| 程序 | 从启动到退出 | `cli.py`（终端）、`ui/server.py`（网页） |
| 会话（Session） | 跨多条用户消息，存成 `.jaxen/sessions/<id>.json`，可恢复 | `agent.py` 的 `CodingAgent` |
| 一次运行（Run） | 一次 `ask()`，内部多轮调用模型 | `agent_loop.py` 的 `AgentLoop` |

在"会话"和"运行"之间还有一层**目标（Goal）**：用户提出一件事，可能要多次运行才做完（中途要澄清、被打断后继续）。
目标的进度由 `ProgressTracker` 记录，跨这几次运行保持。

## 二、"任务"类概念：按生命周期分开，互不依赖

| 概念 | 活多久 | 是什么 | 文件 | 谁驱动 | 工具集 |
|---|---|---|---|---|---|
| **进度账本** `ProgressTracker` | 一个目标 | 目标、状态、todo、代码改了测没测、未解决的失败、未验证的结论、最近的观察 | `progress.py` | harness 自动维护 | — |
| **todo** | 一个目标 | 模型写给自己的计划清单，存在进度账本里，每轮显示在提醒中 | `progress.py`（`set_todos`） | 模型调用 `todo_write` | core |
| **结论登记** | 一个目标 | 模型登记"结论 + 证据"，未验证的结论会拦交付 | `progress.py`（`record_claim`） | 模型调用 `record_claim` | full |
| **运行状态** `RunState` | 一次运行 | 次数、步数、停止原因、最终答案 | `run_state.py` | harness | — |
| **长任务待办** `Backlog` | 跨会话 | 带依赖、验收标准、证据的待办项，存在 `.jaxen/backlog/` | `services/backlog.py` | 模型调用 `backlog_*` | full |
| **后台命令** `BackgroundJobs` | 一条命令 | `run_shell(background=true)` 的慢命令，跑完以 `<job_notification>` 通知 | `services/background_jobs.py` | 模型调用 `run_shell` | core |
| **定时提示** `PromptScheduler` | 跨会话 | 用户安排"到点发给 agent 的一条指令"，存在 `.jaxen/schedules.json` | `services/scheduler.py` | **用户**在 CLI 里 `/schedule` | 不给模型 |

边界规则：
- 短任务（当前请求）的计划只用 todo；长任务（跨会话）只用 backlog。**待办没做完不会拦当前请求的交付**。
- 交付门禁只看进度账本：未解决的可重试失败、未验证的结论、待回答的问题、还在跑的后台命令。
- 定时提示到点时，CLI 把这条指令当成用户消息调用 `agent.ask()`，agent 不知道定时器存在。
- 评测代码里的"task"指评测题目（benchmark task），和上面这些无关。

## 三、"记忆 / 证据"类概念

| 概念 | 活多久 | 是什么 | 文件 |
|---|---|---|---|
| **工作集** `WorkingSet` | 会话 | 当前关注的文件路径和任务摘要（不是记忆，只是"手头在看什么"） | `memory/working_set.py` |
| **源码证据** `SourceEvidence` | 一个目标 | 读过的文件、行范围、新鲜度指纹，避免依赖会被裁剪的聊天历史 | `source_evidence.py` |
| **进度观察** | 一个目标 | 进度账本里最近的工具结果摘要（`observations`），给提醒用 | `progress.py` |
| **长期记忆** `LongTermMemory` | 跨会话 | 可编辑、带来源的稳定事实（偏好、约定、决定），进 system | `memory/long_term.py`，捕获规则在 `memory/learner.py`、`memory/policy.py` |
| **会话归档** `SessionArchive` | 跨会话 | 所有会话消息的 SQLite 全文索引，供 `session_search` 检索 | `memory/session_archive.py` |
| **技能** | 跨会话 | 验证成功的流程沉淀成 `SKILL.md` | `skills/store.py`、`skills/learner.py` |
| **检查点** | 每个工具步 | 恢复用快照 + 工作区漂移检测 | `checkpoint.py` |

## 四、子 agent

| 名字 | 谁触发 | 做什么 | 文件 |
|---|---|---|---|
| 调查子 agent（工具 `investigate`） | 模型自愿 | 只读的大范围调查，只把结论带回主上下文 | `tools.py`（`tool_investigate`）、`subagent.py` |
| 审查子 agent | harness 强制（改过源码、准备交付时） | 只读复查 diff，提出问题就带回主循环一次 | `review.py`、`subagent.py` |

## 五、一次请求的调用链

```text
cli.py / ui/server.py / jsonl_protocol.py
  └─ CodingAgent.ask()                      agent.py
       └─ AgentLoop.run()                   agent_loop.py
            ├─ ProgressTracker.begin(目标)   progress.py
            ├─ 每轮：ContextManager.prepare()  context_manager.py ← prompt_prefix.py（稳定 system）
            │        模型客户端 complete()     providers/
            │        CodingAgent.parse()       工具调用 / 最终答案 / 澄清 / 重来
            │        ToolExecutor.execute()    tool_executor.py → tools.py → workspace.py / security.py
            │          → ProgressTracker.observe_tool()、SourceEvidence、WorkingSet、检查点
            └─ 交付：交付门禁（ProgressTracker.delivery_blockers）→ 审查子 agent → 交付
  落盘：SessionStore（会话）、RunStore（run_state / trace / transcript / report）
```

## 六、模块清单

| 层 | 模块 | 职责 |
|---|---|---|
| 入口 | `__main__.py`、`cli.py` | `python -m jaxen`；参数、终端输入、`/命令`、定时提示 |
| | `ui/server.py`、`ui/web/` | 网页界面：每轮启动一次 `python -m jaxen --jsonl` 并转发事件 |
| | `jsonl_protocol.py` | 非交互机器接口（评测、网页界面共用） |
| 核心 | `agent.py` | `CodingAgent`：装配模型、工具、上下文、状态；`ask()`、`parse()` |
| | `agent_loop.py` | 单次运行的循环、错误恢复、交付门禁与审查 |
| | `progress.py`、`run_state.py` | 进度账本（目标级）、运行状态（运行级） |
| 上下文 | `prompt_prefix.py` | system 的稳定部分：规则、工具策略、项目指令、环境快照 |
| | `context_manager.py` | 组装请求（system + 只追加历史 + 末尾提醒），按预算压缩 |
| | `source_evidence.py`、`memory/` | 源码证据、工作集、长期记忆、会话归档 |
| 工具 | `tools.py` | 工具注册表、参数校验、各工具实现 |
| | `tool_executor.py`、`tool_context.py` | 工具执行总闸口（校验→审批→执行→裁剪→记账）、传给工具的受限上下文 |
| | `workspace.py`、`security.py` | 工作区快照与截断工具函数、路径锚定与脱敏 |
| 服务 | `services/` | 后台命令、长任务待办、定时提示（见第二节） |
| 子 agent | `subagent.py`、`review.py` | 子 agent 运行与结果信封；审查子 agent |
| 模型 | `providers/` | OpenAI 兼容、Anthropic、脚本化测试模型 |
| 落盘 | `session_store.py`、`run_store.py`、`checkpoint.py` | 会话、单次运行工件、检查点 |
| 输出 | `events.py`、`terminal_ui.py`、`markdown_console.py` | 事件接口、终端显示、Markdown 渲染 |
| 配置 | `config.py` | `.env` 与环境变量（`JAXEN_*`） |
| 技能 | `skills/` | 技能存储与自动沉淀 |
| 评测 | `evaluation/` | 不参与运行：判分、指标、确定性套件、故障注入、压缩评测、摘要保真度 |

## 七、命名整理对照（2026-11-05）

| 原来 | 现在 | 为什么改 |
|---|---|---|
| 原项目名下的包名、环境变量前缀、状态目录 | `jaxen`、`JAXEN_*`、`.jaxen/` | 项目改名 Jaxen |
| `task_state.py` / `TaskState` | `progress.py` / `ProgressTracker` | 和"任务系统"同名但不是一回事；它是目标级的进度账本 |
| 进度里的 `task_id` / `evidence` | `goal_id` / `observations` | 和待办项 id、源码证据重名 |
| `services/project_tasks.py`、`create_task` 等 7 个工具 | `services/backlog.py`、`backlog_add/list/claim/complete/fail/block` | 明确是跨会话的长任务；不再拦当前请求的交付 |
| `record_evidence` 工具 | `record_claim` | 它登记的是当前请求的结论，属于进度账本 |
| `services/background_tasks.py`、`<task_notification>` | `services/background_jobs.py`、`<job_notification>` | 只是一条后台命令 |
| `services/cron_scheduler.py`、`schedule_cron` 等 3 个工具 | `services/scheduler.py`、CLI 的 `/schedule` `/schedules` `/unschedule` | 定时是用户功能，模型不需要也不该自己建定时 |
| `delegate` 工具 | `investigate` | 名字说清楚它只做只读调查 |
| `memory/working.py` / `WorkingMemory` / `agent.memory` | `memory/working_set.py` / `WorkingSet` / `agent.working_set` | 是"手头在看的文件"，不是记忆 |
| `memory/curated_store.py` / `CuratedMemoryStore` / `DurableMemoryLearner` | `memory/long_term.py` / `LongTermMemory` / `LongTermMemoryLearner` | 同一个东西原来有 curated、durable 两个叫法 |
| `evidence.py` / `EvidenceLedger` | `source_evidence.py` / `SourceEvidence` | 和进度里的 evidence 重名 |
| `streaming.py` | `events.py` | 里面是事件接口，不是 token 流 |
| `evaluation/metrics.py` | `evaluation/deterministic_suites.py` | 里面是确定性实验套件，不是指标计算 |
| 提醒里的 `Task progress:` | `Goal progress:` | 和 `Run progress:` 对应 |

### 有意保留的

| 名字 | 原因 |
|---|---|
| `evaluation/*_v2.py` | 对应评测数据的 schema 版本，数据文件也带 v2 |
| 评测数据里的标识（套件 id、fixture 包名、canary 值）和 `artifacts/` 里的运行名 | 数据被 fixture 摘要钉住、证据文件不改；只出现在数据和指向这些文件的说明里 |
| `CodingAgent`、`AgentLoop` | 名字和职责一致 |
