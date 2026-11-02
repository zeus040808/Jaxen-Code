# HH Agent Pro 记忆系统 V2 优化总结

> 本文只讨论 `hh_agent_pro`。设计参考来自用户提供的 `Hermes-Agent记忆机制设计参考.txt`，实现仍遵守 HH Agent Pro 的“小、透明、能讲清楚”目标，没有照搬 Hermes 的所有基础设施。

## 1. 结论先行

旧版最主要的问题不是缺少向量数据库，而是不同职责混在一起：Session JSON、working memory、任务状态、Markdown 长期记忆、checkpoint 和 trace 都在保存“过去发生过什么”的某个变体，却没有回答以下基本问题：

- 哪些是当前任务临时状态，哪些是下次 Session 仍应相信的稳定事实？
- 事实写错后如何纠正，用户要求遗忘时如何删除？
- 事实来自用户、工具验证，还是 Agent 推断？
- 某个项目的约定为什么不会泄漏到另一个项目？
- 历史很多时，除人工打开整个 Session JSON 外如何定位相关片段？
- 记忆在一次 Session 中途变化时，提示词是否会产生不可解释的漂移？

V2 将这些问题拆成四个明确层次：

| 层次 | 负责什么 | 存储 | 进入 prompt 的方式 | 如何删除 |
| --- | --- | --- | --- | --- |
| Working memory | 当前 Session 的任务摘要、最近文件、文件短摘要、少量可复用事实 | Session JSON 的 `memory` | 每轮注入仪表盘；按当前问题召回 3 条相关笔记 | Session reset、上限淘汰、文件修改使摘要失效 |
| Task state | 子任务、证据、阻塞、下一步 | Session JSON 的 `task_state` + 项目 task store | 作为执行状态单独渲染 | 由 task 生命周期管理，不再冒充长期记忆 |
| Curated durable memory | 稳定偏好、约定、决策、依赖、纠正和验证事实 | `.hh/memory/curated.json` | Session 创建时生成冻结快照，每轮复用 | `replace` / `remove` / 原子 `batch` |
| Session archive | 跨 Session 的完整历史检索和工具调用取证 | `.hh/sessions/archive.sqlite3`，SQLite + FTS5 | 不默认塞入 prompt；按需 `session_search`，再 `session_read` 展开上下文 | Session JSON 是权威记录；索引可重建 |

旧 Markdown `.hh/memory/topics/*.md` 没有被删除：第一次创建 V2 store 时会无损导入 `curated.json`，旧的自动 promotion 还会双写一段兼容期，避免既有 benchmark 和用户数据突然失效。

## 2. 从 Hermes 学到了什么，又没有照搬什么

采用的核心原则：

1. **状态分层，而不是都叫 memory。** 当前运行状态、精炼事实、用户偏好、完整历史和权威实时状态是不同概念。
2. **精选记忆必须可编辑。** 只有 append 会让冲突越来越多；所以需要确定性的 add / replace / remove / batch。
3. **来源与作用域是一等字段。** 事实没有来源和适用边界，就不能稳定地复用。
4. **Session archive 是按需检索，不是默认灌入。** 搜索结果必须带来源地址，随后能读取周边顺序事件。
5. **写入与注入解耦。** 写入立即持久化，但当前 Session 的系统上下文保持冻结，下一 Session 才看到新事实。
6. **容量超限显式失败。** 不偷偷用 LRU 删除用户不知道的事实，而是要求先整理或遗忘旧事实。
7. **历史事实不是实时权威。** prompt 明确要求对会变化的工作区事实再次使用工具验证。

本次刻意没有加入：

- 向量数据库：当前事实数量很小，结构化边界与 CRUD 的收益远大于 embedding。
- 知识图谱：项目还没有实体关系查询需求，会显著增加维护成本。
- LLM 自动整理/合并：不确定性太高，先把确定性 CRUD 和准入做好。
- 完整 Hermes profile/skills 系统：`AGENTS.md` 和现有 workspace context 已承担项目规则与流程知识。

## 3. 旧设计为什么显得混乱

### 3.1 一份 working memory 有两套字段

旧 `default_memory_state()` 同时保存：

```text
working.task_summary  <-> task
working.recent_files  <-> files
episodic_notes        <-> notes
```

每次 normalize 又把两边互相同步。这不是兼容层，而是运行时的双重真相；调用者可以只改其中一边。V2 仍能读取旧 Session 字段完成迁移，但规范化后会删除 `task/files/notes`，运行时只写结构化字段。

### 3.2 过程失败与可复用事实混在 `episodic_notes`

`read_file error; check path` 与 `数据库迁移统一使用 Alembic` 的生命周期和价值完全不同。V2 新增 `process_notes`：

- `episodic_notes`：当前 Session 中可能再次需要的事实；
- `process_notes`：失败、partial success、下一步重试提醒；
- 两者各自有容量上限，并在加载旧 Session 时自动拆分。

它们仍能参与当前问题召回，但在存储和统计上不再混为一谈。

### 3.3 Markdown 长期记忆只能追加或猜 subject

旧 `DurableMemoryStore` 固定四个 topic，通过正则猜一条文本的 subject，再决定是否覆盖。这会产生几个问题：

- 没有稳定 ID；
- 不能让用户精确 list/remove；
- 没有来源、置信度、版本；
- 没有 workspace scope；
- 一批写入中途失败时没有全有或全无保证；
- Markdown 写入不是原子替换，也没有跨进程锁。

V2 的 `MemoryEntry` 把这些字段变成显式模型：

```python
MemoryEntry(
    id="mem_...",
    namespace="memory | user",
    scope="workspace | project | global_user",
    scope_key="...",
    kind="preference | convention | decision | dependency | fact | correction",
    content="...",
    provenance="explicit_user | user_correction | verified_tool | agent_inference | legacy_import",
    confidence=1.0,
    version=1,
    source_session_id="...",
    source_message_id="...",
)
```

### 3.4 Session history 可读，但不能发现

用户之前对 trace 的质疑成立：如果只想顺序检查一次运行，Session history 通常更直观。问题发生在跨几十个 Session 寻找“之前在哪里说过 SQLite”时，逐文件阅读不可扩展。

V2 没有拿 SQLite 替代 Session：

- Session JSON 仍是权威、完整、适合人工顺序阅读的记录；
- SQLite 只是可重建的 discovery index；
- `session_search` 返回 `session_id / message_id / sequence_no / role / tool name`；
- `session_read` 可围绕命中的 message ID 展开按顺序排列的用户、助手和工具事件。

因此 archive 补足的是“发现与定位”，不是再造一份 trace。

## 4. 关键实现

## 4.1 精选记忆 CRUD 与原子提交

核心实现在 `hh_agent_pro/memory/curated_store.py`。

`apply_batch()` 的流程是：

```text
拿跨线程锁 + 跨进程文件锁
        ↓
重新读取磁盘最新 revision
        ↓
在内存副本上依次验证并应用全部操作
        ↓
检查 namespace 容量
        ↓
写临时文件 + flush + fsync + os.replace
```

任何一步失败都不会执行最终 `_write()`，因此不会出现“前两条写进去了，第三条失败”的半状态。

`replace/remove` 使用**唯一子串匹配**：

- 0 条：`MemoryNotFound`；
- 1 条：确定执行；
- 多条：`AmbiguousMemoryMatch`，要求调用者提供更精确的 match。

替换保持同一个 `id`，递增 `version`，并保留原 `created_at`。这样纠正是同一事实的演化，而不是留下两个冲突事实。

容量按 namespace 计算，默认：

- `user`: 1375 字符；
- `memory`: 2200 字符。

容量超限抛出 `MemoryCapacityError`，不会静默驱逐旧事实。

## 4.2 准入与安全

`hh_agent_pro/memory/policy.py` 负责验证：

- namespace、scope、kind、provenance 的允许集合；
- workspace/project 必须提供 `scope_key`；
- confidence 必须在 `[0, 1]`；
- 单条内容最大 800 字符；
- 控制字符、私钥、API key/token/password 形态；
- 中英文 prompt injection 形态。

这层同时用于显式 `memory_update` 和旧的 final-answer promotion，避免兼容入口绕过新安全规则。

这里的安全扫描是确定性第一道门，不应声称能识别所有间接提示注入。真正的纵深防御还包括：只允许有限 kind、冻结注入、prompt 中声明历史不是指令、以及工具执行前的既有审批护栏。

## 4.3 冻结快照注入

`CodingAgent.__init__()` 创建 `CuratedMemoryStore` 后立即：

```python
self.curated_memory_snapshot = self.curated_memory.render_snapshot()
```

之后 `durable_memory_text()` 只返回这份字符串。`memory_update` 会立即落盘，但工具结果明确说明“next session 生效”。好处是：

- 同一个 Session 中 system context 不会因为 Agent 自己写了记忆而漂移；
- 写入成功不等于当前推理立刻接受新事实；
- 新 Session 和恢复后重新构造 `HH` 时自然刷新。

快照分为：

- `User profile`：`global_user` 范围的用户偏好；
- `Workspace facts and decisions`：当前 `scope_key` 对应的事实；
- 一条实时验证规则：存储事实是 context，不是 live authority。

## 4.4 Session archive

`hh_agent_pro/memory/session_archive.py` 建立三张逻辑表：

```text
sessions
messages
messages_fts (FTS5 virtual table)
```

每次 `CodingAgent.record()` 的顺序是：

1. 分配单调递增的 `archive_sequence`；
2. 追加到 Session history；
3. 写入 archive，得到 `archive_message_id`；
4. 保存 Session JSON。

若 archive 写入失败，Session JSON 仍会保存，错误放在 `last_archive_error`；下次创建 `HH` 时 `sync_session()` 会根据 `(session_id, archive_sequence)` 幂等补齐。这体现了“Session 是权威，索引可重建”。

FTS 索引内容包含 `tool name + args JSON + result content`，因此 `run_shell` 和命令参数也可发现。中文无空格文本在 FTS5 `unicode61` 下不总能按用户期望切词，搜索无结果时会使用参数化 `LIKE` 兜底。

所有 SQLite 操作使用显式 `_open()` 上下文，在 commit/rollback 后 `close()`；这是独立对照脚本在 Windows 上发现并修复的资源占用问题。

## 4.5 中文 working-memory 召回

旧 tokenizer 只有：

```python
re.findall(r"[A-Za-z0-9_]+", text)
```

所以纯中文 query 和 note 都得到空集合。V2 对连续中文生成：

- 完整连续串；
- 单字 token；
- 双字 token；
- 英文/数字仍沿用原逻辑。

这是简单、透明的本地方案。它不会像 embedding 那样理解复杂同义词，但已经修复中文项目最明显的零召回缺陷，且无需额外服务。

## 4.6 Agent 工具

主 Agent 新增四个工具，子 Agent 不获得持久写权限：

- `memory_list(namespace='')`：列出当前作用域可见条目及 ID、来源、置信度、版本；
- `memory_update(action=...)`：add / replace / remove / batch，是 risky tool，需要现有审批机制；
- `session_search(query, ...)`：跨 Session 发现历史，可选择是否包含工具；
- `session_read(session_id, around_message_id=...)`：按顺序展开历史窗口。

prompt 同时加入准入规则，告诉模型不要存 secret、原始工具输出、临时 run state 或覆盖系统规则的文本；纠正必须 replace/remove，不得追加冲突事实。

## 5. 数据迁移和兼容性

### 5.1 旧 Session memory

`normalize_memory_state()` 会：

1. 若新字段为空，从 `task/files/notes` 读取旧值；
2. 将 `kind=process` 的旧 episodic note 移入 `process_notes`；
3. 删除运行时重复字段 `task/files/notes/durable_topics`；
4. 保存时只保留 V2 working-memory schema。

### 5.2 旧 Markdown durable memory

如果 `.hh/memory/curated.json` 尚不存在，`HH` 初始化时读取四个旧 topic：

- project-conventions → `memory/convention/workspace`；
- key-decisions → `memory/decision/workspace`；
- dependency-facts → `memory/dependency/workspace`；
- user-preferences → `user/preference/global_user`。

来源标为 `legacy_import`，置信度为 `0.8`。旧文件不会删除。

### 5.3 旧 promotion

原来要求最终答案输出 `Decision:`、`Preference:` 等格式的 benchmark 仍然工作。promotion 会继续写旧 Markdown，同时写入 curated store；同 subject 的更新尝试转换为 V2 replace。

## 6. 测试与结果

## 6.1 新增专项测试

文件：`tests/test_memory.py`

最终 `16 passed`，覆盖：

1. 纠正保持 ID、递增版本、记录 `user_correction`；
2. 精确遗忘；
3. 模糊匹配拒绝；
4. batch 任一步失败时完整回滚；
5. 容量超限拒绝且不静默淘汰；
6. 24 个并发 writer 不丢更新，JSON revision 正确；
7. workspace 作用域隔离；
8. global user preference 跨 workspace；
9. secret 和中英文 prompt injection 拒绝；
10. 冻结快照只在下一 Session 生效；
11. session search 来源定位、工具可选过滤、around read；
12. 中文召回与 process note 分离；
13. 旧 Markdown 无损迁移和新旧能力对照；
14. curated batch 被容量规则拒绝时，不产生旧 Markdown 半写入。
15. working / durable / task state 以三个独立 prompt section 注入；
16. 旧 `memory` 总预算配置可按兼容比例拆给三个 V2 section。

## 6.2 全量回归

命令：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

结果：

```text
137 passed, 1 warning in 26.69s
```

警告是工作区原有的 `.pytest_cache` 写权限问题：`PytestCacheWarning: WinError 5`，不影响测试结果，也不是本次代码引入。

在新增 `session_read` 后又重跑记忆专项 + Pro 扩展 + reliability：

```text
45 passed, 1 warning in 55.07s
```

## 6.3 旧 working-memory 行为实验

为确认结构清理没有削弱原先“避免重复读文件”的收益，运行 12 类任务、每变体 12 次的 deterministic ablation：

| 变体 | repeated reads | avg tool steps | correct rate | memory hit rate |
| --- | ---: | ---: | ---: | ---: |
| memory_on | 0 | 0.0 | 100% | 100% |
| memory_off | 12 | 1.0 | 100% | 0% |
| memory_irrelevant | 12 | 1.0 | 100% | 0% |

结果在 `artifacts/memory-ablation-v2-after-memory-v2.json`。它说明旧 working-memory 的已有收益被保留；它不证明真实模型上的语义记忆质量。

## 6.4 V1/V2 能力对照

脚本：

```powershell
.venv\Scripts\python.exe scripts\run_memory_v2_comparison.py
```

结果在 `artifacts/memory-v2-comparison.json`：

| 确定性能力 | V1 | V2 |
| --- | --- | --- |
| 中文本地召回 | 否 | 是 |
| 显式 CRUD | 否 | 是 |
| 原子 batch | 否 | 是 |
| scope 隔离 | 否 | 是 |
| provenance + version | 否 | 是 |
| frozen snapshot | 否 | 是 |
| 可定位的 Session search | 否 | 是 |
| prompt-injection 持久化拒绝 | 否 | 是 |

能力探针结果是 `V1 0/8，V2 8/8`。这里必须正确解读：它表示 V2 新增并验证了八项旧版**不存在的确定性能力**，不是说“整体 Agent 能力提高 800%”，也不是与 Hermes 的效果对标。

## 7. 哪些核心代码发生了变化

### 新增

- `hh_agent_pro/memory/models.py`：数据对象和确定性错误类型；
- `hh_agent_pro/memory/policy.py`：准入、安全扫描、中英文 tokenizer；
- `hh_agent_pro/memory/curated_store.py`：作用域、CRUD、唯一匹配、版本、容量、锁和原子提交；
- `hh_agent_pro/memory/session_archive.py`：SQLite schema、同步、append、FTS/LIKE search、around read；
- `hh_agent_pro/evaluation/memory_v2.py`：V1/V2 能力探针；
- `scripts/eval/run_memory_ablation.py`：可重复生成对照 artifact；
- `tests/test_memory.py`：专项行为和不变量测试。

### 修改

- `hh_agent_pro/features/memory.py`：移除运行时重复字段，拆分 process note，中文召回，旧 durable 不再参与动态检索；
- `hh_agent_pro/context_manager.py`：working、durable、task state 分成独立 prompt section 和预算，并兼容旧 `memory` 总预算配置；
- `hh_agent_pro/agent.py`：创建 curated/archive、冻结注入、历史双写与恢复、旧 promotion 迁移、ToolContext 接线；
- `hh_agent_pro/tools.py`：四个记忆/历史工具及校验；
- `hh_agent_pro/tool_context.py`：向工具提供受限 memory/archive 依赖；
- `hh_agent_pro/prompt_prefix.py`：精选记忆准入、纠错、冻结和历史证据规则。

## 8. 对 `run_state`、trace 与 Session history 的最终定位

这次没有为了“顺手整理”而大改 `run_state` 和 trace，因为它们属于运行控制/可观测性，不应和记忆重构绑成一次巨大改动。但边界可以明确：

- `run_state`：当前一次 run 的控制状态，回答为什么停止、工具步数、checkpoint、恢复状态；不是记忆，不应注入为长期事实。
- `trace`：机器可消费的逐事件诊断日志；当前人工体验确实不如 Session history。
- `Session history`：一次会话最直观、最权威的顺序记录，人工排查优先看它。
- `Session archive`：在多个 Session 之间做 discovery 和 around-read，解决 history 没有索引的问题。

因此你的判断“直接看 Session history 更清晰”在单 Session 人工检查上是对的。V2 的改进方向不是强迫你看 trace，而是让 Session history 保持权威，同时获得跨 Session 搜索和精确展开能力。

## 9. 已知边界与下一步

当前实现已经是稳定、可讲清楚的 V2，但仍有边界：

- 中文 tokenizer 是字/双字重叠，不理解复杂同义词；事实数量显著增长后再考虑 BM25 调权或 embedding。
- prompt-injection 检测是规则扫描，不可能覆盖所有语义攻击；高风险工具仍必须依赖执行层审批。
- 旧 Markdown 双写是迁移期策略，未来应在版本升级后停止双写，只保留一次性导入和只读导出。
- `global_user` 当前保存在 workspace 的 `.hh/memory` 下，所以“跨 workspace”是同一个 store root 的语义；若要真正跨仓库共享，应将 user profile 单独放到用户级配置目录，并明确隐私边界。
- archive 当前提供 search/read，没有单独做 CLI 命令；Agent 已可通过工具使用，后续可增加人类友好的 `hh sessions search/read`。
- 未使用真实 DeepSeek/通义做长期对话语义评估；本次结论只基于确定性能力测试、全量回归和 scripted ablation，不夸大为真实模型质量证明。

## 10. 最终判断

相较旧版，这不是“多存了一种 JSON”，而是把记忆系统从混合缓存改成了明确的生命周期：

```text
工具与对话事件
    ├─ 完整记录 → Session JSON（权威）→ SQLite archive（发现/定位）
    ├─ 当前任务复用 → working / episodic / process memory
    └─ 明确准入 → curated durable memory
                       ├─ scope
                       ├─ provenance
                       ├─ correction / forget
                       ├─ atomic batch
                       └─ next-session frozen injection
```

最实质的提升是：记忆现在可以回答“这是什么、从哪来、对谁有效、什么时候注入、错了怎么改、忘了怎么删、失败会不会半写入、旧历史如何定位”。这正是旧设计最缺失的稳定性和清晰度。
