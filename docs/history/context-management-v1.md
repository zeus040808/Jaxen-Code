# Jaxen Code 上下文压缩与管理机制详解

> 本文对应当前仓库实现，核心代码位于：
>
> - `jaxen/context_manager.py`
> - `jaxen/source_evidence.py`
> - `jaxen/agent_loop.py`
> - `jaxen/agent.py`
> - `jaxen/tool_executor.py`
> - `jaxen/prompt_prefix.py`
> - `jaxen/providers/clients.py`

## 1. 这次为什么必须修改

旧设计把 Session History 同时当成了三种东西：

1. 完整审计记录；
2. 模型下一轮的短期上下文；
3. 读文件后得到的代码证据。

三种职责放在同一个长文本里，会产生一个必然问题：History 预算有限，读到的两个大文件无法同时保留。旧实现对最近 12 条历史每条最多保留约 11,000 字符，而整个 History 只有 12,000 字符，所以会出现：

```text
read_files(memory/)       -> memory/ 内容进入 Prompt
read_file(features/...)   -> 前一份内容被挤出 Prompt
再次 read_files(memory/)  -> features/memory.py 被挤出
再次 read_file(features/) -> memory/ 又被挤出
```

这不是模型“不够聪明”，而是 Harness 在每轮重新组装 Prompt 时，把模型刚获得的关键证据删掉了。

新设计的核心原则是：

```text
Session History 是完整审计日志，不是唯一的模型记忆。
Source Evidence 是当前任务不可丢失的代码证据。
Working Memory 是小型工作集。
Durable Memory 是跨 Session 稳定事实。
Task State / Run State 是控制数据，不是代码证据。
```

---

## 2. 当前上下文系统的分层

当前上下文由八个明确分区组成，顺序定义在 `context_manager.py`：

```python
SECTION_ORDER = (
    "prefix",
    "working_memory",
    "long_term_memory",
    "progress",
    "evidence",
    "relevant_memory",
    "history",
    "current_request",
)
```

各分区的职责如下。

| 分区 | 职责 | 是否持久化 | 是否允许压缩 |
|---|---|---:|---:|
| `prefix` | Agent 身份、规则、运行环境、工具策略、仓库导航 | 根据 workspace 重建 | 工作区快照可压缩，核心规则受保护 |
| `working_memory` | 当前任务摘要、最近文件、文件短摘要 | Session | 是 |
| `long_term_memory` | 用户偏好、项目约定、长期决策等稳定事实 | `.jaxen/memory` | 是 |
| `progress` | 目标、子任务、失败、开放问题、最小 Run 进度 | Session | 是，但优先级较高 |
| `evidence` | 当前任务读过的源文件结构、范围、freshness、证据 ID | Session 的 `source_evidence` | 是，但拥有较高保底预算 |
| `relevant_memory` | 根据当前问题召回的最多三条相关记忆 | 每轮动态检索 | 优先压缩 |
| `history` | 最近对话和工具事件，用于局部连续性 | Session / Archive | 强压缩 |
| `current_request` | 当前用户请求及 final-only 指令 | 当前调用 | 永不裁剪 |

这里最关键的变化是新增 `evidence`。它不是 Durable Memory，也不是 ProgressTracker 中泛化的“任务进展证据”，所以 Session 字段明确命名为：

```python
self.session["source_evidence"]
```

这样避免项目里再次出现多个都叫 `memory` 或多个都叫 `evidence` 的模糊对象。

---

## 3. Source Evidence Ledger：代码证据账本

### 3.1 为什么不能只保留工具原文

工具原文往往很大，而且大部分字符对下一步决策没有同等价值。模型比较两个 Python 模块时，最需要长期保留的是：

- 文件路径；
- 模块职责；
- 类名；
- 顶层函数；
- 关键导入；
- 读取范围是否完整；
- 文件是否在读取后发生变化。

这些信息由 `jaxen/source_evidence.py` 的 `SourceEvidence` 负责保存。

### 3.2 Evidence 的状态结构

核心结构是：

```python
def default_source_evidence_state():
    return {
        "schema_version": "v1",
        "task_id": "",
        "next_id": 1,
        "files": {},
        "actions": [],
        "updated_at": now(),
    }
```

`files` 按规范化路径去重。一次文件证据大致如下：

```json
{
  "id": "E2",
  "path": "jaxen/features/memory.py",
  "summary": "purpose=...; classes=DurableMemoryStore, LayeredMemory; functions=...; imports=...",
  "start": 1,
  "end": 200,
  "total_lines": 650,
  "complete": false,
  "freshness": "完整文件 sha256",
  "source_tool": "read_file",
  "updated_at": "..."
}
```

`complete` 很重要。读取 `1-200` 行但文件共有 650 行时，Evidence 会明确标记为 `partial`，模型不会误以为自己已经检查了整个文件。

### 3.3 Python 结构摘要如何生成

`summarize_source_text()` 优先使用 Python 标准库 `ast`，不额外调用 LLM：

```python
tree = ast.parse(text)
doc = ast.get_docstring(tree, clean=True) or ""
classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
functions = [
    node.name
    for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
]
```

同时收集模块级 import：

```python
for node in tree.body:
    if isinstance(node, ast.Import):
        imports.extend(alias.name for alias in node.names)
    elif isinstance(node, ast.ImportFrom):
        module = ("." * int(node.level)) + str(node.module or "")
        imports.append(module)
```

最后得到确定性摘要：

```text
purpose=Current task working set;
classes=WorkingSet;
functions=render_context, set_file_summary;
imports=.memory.policy, .workspace;
excerpt_sha256=4ccf17...
```

为什么不用 LLM 生成摘要：

- 不增加一次 API 调用；
- 不引入摘要幻觉；
- 相同代码得到相同结果；
- 单元测试可以精确断言；
- 对这个学习项目更容易逐行理解。

非 Python 文件会退化到类/函数正则、Markdown 标题或前几个有效内容行。

### 3.4 `read_files` 如何逐文件写 Evidence

旧实现处理 `read_files` 时只调用 `remember_file()`，然后立刻 `return`，导致文件内容没有被提炼。

现在 `agent.py` 中统一走 `_remember_source_evidence()`：

```python
if paths:
    for item in paths:
        self._remember_source_evidence(name, item, args)
    self.session["working_set"] = self.working_set.to_dict()
    self.session["source_evidence"] = self.source_evidence.to_dict()
    return
```

每个路径都会重新读取对应范围、生成摘要并记录：

```python
summary = summarize_source_text(canonical, excerpt)
self.working_set.set_file_summary(canonical, summary)
return self.source_evidence.record_file(
    canonical,
    summary,
    start=start,
    end=actual_end,
    total_lines=len(lines),
    source_tool=name,
)
```

所以一次 `read_files([a.py, b.py, c.py])` 会形成 `E1/E2/E3` 三条独立证据，而不是一个不可拆分的 11K 文本块。

### 3.5 Freshness 如何避免旧代码污染 Prompt

Evidence 保存的是完整文件 SHA-256：

```python
def _freshness(self, path):
    resolved = (self.root / path).resolve()
    return hashlib.sha256(resolved.read_bytes()).hexdigest()
```

渲染和复用前会重新计算：

```python
if item.get("freshness") != self._freshness(canonical):
    return ""
```

因此文件被 `write_file`、`patch_file` 或外部编辑后，旧摘要不会继续作为当前事实注入。

---

## 4. 当前 Prompt 的预算策略

### 4.1 总预算与分区预算

默认总预算仍为 24,000 字符：

```python
DEFAULT_TOTAL_BUDGET = 24000
```

新的初始分区预算是：

```python
DEFAULT_SECTION_BUDGETS = {
    "prefix": 5200,
    "working_memory": 850,
    "long_term_memory": 500,
    "progress": 950,
    "evidence": 5200,
    "relevant_memory": 700,
    "history": 7200,
}
```

它们合计超过 24K 并不矛盾，因为这是每个分区的局部上限；实际内容通常远低于上限。如果最终拼接超过总预算，才进入全局收缩。

新的资源偏好是：

```text
Source Evidence 5.2K > History 7.2K 中任意单条工具结果 2.6K
```

含义不是 Evidence 比 History 总量更大，而是：代码事实不能再被一个最新的大工具输出整体覆盖。

### 4.2 全局超限时的牺牲顺序

```python
DEFAULT_REDUCTION_ORDER = (
    "relevant_memory",
    "long_term_memory",
    "working_memory",
    "history",
    "progress",
    "evidence",
    "prefix",
)
```

逐项解释：

1. `relevant_memory` 是检索增强信息，和当前任务直接证据相比更容易舍弃；
2. `long_term_memory` 是长期背景，不一定与当前下一步有关；
3. `working_memory` 与 Evidence 有一定重复；
4. `history` 的旧原文已由 Evidence/ProgressTracker 提炼；
5. `progress` 决定是否还能安全交付，后期才压；
6. `evidence` 是当前代码判断的核心依据，拥有较高保底；
7. `prefix` 最后压，而且核心规则段受到特殊保护。

当前请求不在收缩列表中，所以永不裁剪。

### 4.3 Prefix 为什么不能直接截尾

Prefix 内有一个明确边界：

```python
COMPRESSIBLE_WORKSPACE_MARKER = "\n\nWorkspace snapshot (compressible):\n"
```

边界前是身份、规则和工具行为契约，边界后才是可以压缩的仓库快照：

```python
def _render_prefix(raw, budget):
    critical, workspace = raw.split(marker, 1)
    protected = critical + marker
    if budget <= len(protected):
        return protected
    return protected + _tail_clip(workspace, budget - len(protected))
```

因此即使 Prefix 预算很小，也宁愿产生少量有界溢出，不能悄悄删除安全规则和工具协议。

### 4.4 Evidence 分区如何压缩

Evidence 不能使用普通的字符串截尾，因为那会只留下前几个文件、删除后面的文件。

`SourceEvidence.render(budget)` 的策略是：

1. 预算足够：显示完整结构摘要、读取范围和已观察调用；
2. 预算不足：所有有效文件平均分配摘要额度；
3. 预算极小：保留最新证据条目；
4. 始终验证 freshness，过期证据不渲染。

核心思路：

```python
per_file = max(60, (budget - overhead) // max(1, len(valid)))
for item in valid:
    prefix = f"  - {item['id']} {item['path']}: "
    summary_budget = max(12, per_file - len(prefix) - 1)
    compact.append(prefix + clip(item["summary"], summary_budget))
```

这保证“比较多个文件”的任务不会因为 Evidence 压缩再次退化成只剩一个文件。

---

## 5. History 现在如何压缩

### 5.1 History 的定位

完整 Session History 仍然写入 Session JSON，并同步到 `archive.sqlite3`，用于：

- 用户直接查看；
- 调试和审计；
- `session_search/session_read` 跨 Session 检索；
- Checkpoint 恢复。

但每轮 Prompt 不再尝试完整回放。

### 5.2 最近窗口由 12 条缩到 6 条

```python
recent_window = 6
recent_start = max(0, len(history) - recent_window)
```

最近 6 条仍有更高优先级，但不同角色有不同单条上限：

```python
if item.get("role") == "tool":
    line_limit = 2600
elif item.get("role") == "assistant" and item.get("tool_call"):
    line_limit = 500
else:
    line_limit = 1200
```

旧设计给最近工具结果 11K，现在降为 2.6K。因为代码结构已经进入 Evidence，History 只需保留最新原文细节，不再承担全部事实。

### 5.3 较旧的 `read_file/read_files` 如何处理

旧读取不会保留完整输出，而是从 Source Evidence 或 Working Memory 取 freshness 有效摘要：

```python
if item["role"] == "tool" and item["name"] in {"read_file", "read_files"}:
    ...
    summary = self._reusable_file_summary(path)
    if summary:
        summaries.append(f"{path} -> {summary}")
```

`read_files` 会按路径拆分，所以批量读取不再是压缩盲区。

重复的旧读取路径会折叠，并写入 Prompt Metadata：

```text
collapsed_duplicate_reads
reused_file_summary_count
summarized_tool_count
```

### 5.4 为什么保留最新原文仍然必要

Evidence 是确定性结构摘要，不能替代所有原文。例如：

- 某个函数的具体条件判断；
- 测试失败的完整错误码；
- Shell 命令最后几行输出；
- 用户对上一步结果的纠正。

所以策略不是“只留摘要”，而是：

```text
结构化 Evidence 保证跨步骤不遗忘；
少量最近原文保证下一步仍能看到实现细节。
```

---

## 6. DeepSeek 原生消息不再是一个巨型 user Prompt

### 6.1 旧请求的问题

旧 DeepSeek 请求是：

```python
"messages": [{"role": "user", "content": prompt}]
```

这个 `prompt` 内同时包含 system 规则、历史、工具结果和用户问题。模型看不到真正的消息角色。

同时 Prefix 已经写了一遍完整工具 schema，API 又通过 `tools=` 发一遍，形成重复。

### 6.2 Provider 能力声明

当前各 Provider 显式声明：

```python
self.supports_native_tools = True   # DeepSeek Chat Completions
self.supports_native_tools = False  # 文本协议或 Codex CLI 边界
```

Runtime 构建 Prefix 时把该能力传入：

```python
return build_prompt_prefix(
    workspace=self.workspace,
    tools=self.tools,
    runtime_info=self.safe_runtime_info(),
    native_tools=bool(getattr(self.model_client, "supports_native_tools", False)),
)
```

原生工具 Provider 的 Prefix 不再展开 23 个工具字段，只保留行为策略：

```text
Tool names and argument schemas are supplied through the provider's native tools field.
Prefer list_files, read_file/read_files, search and runtime_info before run_shell.
```

### 6.3 DeepSeek 的实际消息结构

`ContextManager.provider_request()` 生成：

```python
{
    "system_prompt": system_prompt,
    "messages": messages,
}
```

最终 HTTP 请求类似：

```json
{
  "messages": [
    {"role": "system", "content": "Jaxen rules and runtime identity"},
    {"role": "user", "content": "Current task context + source evidence"},
    {"role": "user", "content": "用户当前问题"},
    {
      "role": "assistant",
      "tool_calls": [{
        "id": "call_123",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{...}"}
      }]
    },
    {
      "role": "tool",
      "tool_call_id": "call_123",
      "content": "工具结果"
    }
  ],
  "tools": ["原生 JSON Schema"],
  "tool_choice": "auto"
}
```

`system`、`user`、`assistant tool_calls` 和 `tool` 的角色不再混在一个文本里。

### 6.4 assistant/tool 必须成对保留

Chat Completions 协议要求 tool result 对应一个之前的 tool call。原生消息也受 7.2K 历史预算控制，但不能把一对消息从中切开。

`_budget_native_messages()` 先把消息组成 unit：

```python
if (
    message.get("role") == "assistant"
    and message.get("tool_calls")
    and messages[index + 1].get("role") == "tool"
):
    units.append([message, messages[index + 1]])
```

然后按 unit 从新到旧装入预算。因此要么同时保留 assistant/tool，要么同时删除，绝不会生成非法请求。

---

## 7. 重复读取缓存与工具步数

### 7.1 缓存键

缓存只用于 `read_file/read_files`，键由两部分组成：

```text
canonical tool name + normalized args + current file freshness
```

核心代码：

```python
def _read_cache_key(self, name, args):
    freshness = self._read_freshness(name, args)
    if not freshness:
        return ""
    return self._call_fingerprint(name, args) + ":" + freshness
```

参数相同但文件已变化时，freshness 不同，缓存自动失效并重新执行工具。

### 7.2 缓存命中返回紧凑证据，而不是完整大输出

```python
content = (
    f"cached observation: unchanged successful {name} result reused; "
    "no tool step consumed.\n"
    + "\n".join(summaries)
)
```

元数据明确记录：

```python
{
    "cache_hit": True,
    "cache_hit_count": cache_hit_count,
    "effective_tool_step": False,
    "recommended_action": "Use the existing source evidence...",
}
```

### 7.3 为什么缓存命中不消耗有效工具额度

AgentLoop 现在在执行后读取 `effective_tool_step`：

```python
effective_step = bool(tool_result.metadata.get("effective_tool_step", True))
if effective_step:
    tool_steps += 1
    run_state.record_tool(name)
else:
    agent.emit_trace(run_state, "tool_cache_hit", {"name": name, "args": args})
```

模型做了一个不理想的重复决定，所以 `attempts` 仍增加；但 Harness 没有真正读取磁盘并产生新证据，因此 `tool_steps` 不增加。

这让两个指标回归各自语义：

```text
attempts   = 调了几次模型
tool_steps = 真正执行了几次有效工具动作
```

### 7.4 为什么不能缓存所有工具

只有纯读取工具适合这种复用。以下调用即使参数相同，也可能因为外部状态变化而应该重新执行：

- `backlog_complete`：之前可能缺 Evidence，后来已经补齐；
- `run_shell`：命令结果可能变化；
- `write_file/patch_file`：重复执行可能有副作用；
- 定时任务和后台任务工具：状态会变化。

因此其他工具保留原有的连续循环保护，不使用读取缓存。

### 7.5 重复缓存读取的软熔断

如果同一个 unchanged read 连续两次命中缓存，说明模型没有利用 Evidence。此时进入 `final_only_mode`：

```python
if cache_hit_count >= 2:
    agent.final_only_mode = True
```

下一次禁止工具，让模型从已有证据作答，避免在缓存上无限循环。

---

## 8. 工具额度耗尽后的 final-only 回合

旧行为是在模型请求第 7 个工具时立即返回：

```text
Stopped after reaching the tool step limit without a final answer.
```

这会浪费前面已经收集的所有证据。

现在首次发现工具额度耗尽时，不立即结束，而是：

```python
agent.final_only_mode = True
notice = (
    "Tool budget exhausted. No further tools will execute. "
    "Use the verified evidence and return final, needs_user_input, or blocked now."
)
continue
```

下一轮 Prompt 增加权威运行指令：

```text
Runtime directive (authoritative):
- Tool execution is disabled for this decision.
- Use the verified evidence already supplied.
- Return exactly one <final>, <needs_user_input>, or <blocked> response now.
```

并且 DeepSeek API 的 `tools` 参数设为 `None`：

```python
tools=None if agent.final_only_mode else provider_tool_schemas(...)
```

如果模型在 final-only 回合仍输出工具调用，才真正以 `step_limit_reached` 停止。

---

## 9. Run State 现在如何注入

完整 `run_state.json` 继续用于：

- 运行中观察；
- Checkpoint；
- Report；
- 恢复；
- 停机原因分析。

但 Prompt 只注入最小控制视图：

```python
Run progress (control data, not source evidence):
- decisions: 4/18
- effective_tools: 3/6
- last_tool: read_file
- final_only: False
```

核心代码位于 `_task_and_run_state_text()`：

```python
lines.extend(
    [
        "Run progress (control data, not source evidence):",
        f"- decisions: {run_state.attempts}/{max_attempts}",
        f"- effective_tools: {run_state.tool_steps}/{max_steps}",
        f"- last_tool: {run_state.last_tool or '-'}",
        f"- final_only: {bool(self.agent.final_only_mode)}",
    ]
)
```

这解决了“run_state 每次变化但对模型没有实际帮助”的问题：模型只看到会影响下一步决策的四个字段，不看到时间戳、ID、checkpoint_id 等噪声。

终端也不再显示含糊的 `step 7/18`，而是：

```text
Thinking - decision 7/18 | tools 6/6 | final-only
```

---

## 10. 一轮请求的完整数据流

### 10.1 Run 开始

`AgentLoop.run()`：

1. 清空本 Run 的读取缓存与失败计数；
2. 设置 Working Memory 的任务摘要；
3. `ProgressTracker.begin()` 决定这是新任务还是延续任务；
4. `SourceEvidence.begin(task_id)`：新逻辑任务则清空，延续任务则保留；
5. 用户消息写入完整 Session History。

### 10.2 Prompt 构建

每次模型决策前：

1. 刷新 workspace fingerprint；
2. 检查 Checkpoint / stale file summary；
3. 收集八个分区原始内容；
4. 每个分区先按局部预算压缩；
5. 如果总 Prompt 仍超过 24K，按牺牲顺序继续压缩；
6. 记录 `prompt_metadata`；
7. 文本 Provider 使用兼容拼接 Prompt；
8. DeepSeek 使用 role-aware structured messages。

### 10.3 模型请求工具

1. 解析或接收原生 tool call；
2. 在 Session 写入 assistant tool-call 事件；
3. ToolExecutor 校验名称、参数、审批和边界；
4. 对读取工具计算 cache key；
5. 命中则返回紧凑缓存证据，不增加有效工具步数；
6. 未命中则执行工具；
7. `read_file/read_files` 按文件写入 Source Evidence；
8. tool result 以对应的 `tool_call_id` 写入 Session；
9. Trace 写 `tool_executed` 或 `tool_cache_hit`；
10. 创建 Checkpoint。

### 10.4 下一轮模型决策

下一轮不需要依赖上一轮完整 11K 工具输出。即使原文被 History 压缩，Prompt 中仍有：

```text
Verified source evidence:
- E1 jaxen/memory/models.py: classes=MemoryEntry...
- E2 jaxen/memory/policy.py: functions=tokenize...
- E3 jaxen/features/memory.py: classes=LayeredMemory...
- already_observed_calls:
  - read_files {...}
  - read_file {...}
```

这就是本次修复阻止“交替重读”的关键。

---

## 11. Prompt Metadata 与可观测性

每次 Prompt 构建都会记录：

```json
{
  "prompt_chars": 18240,
  "prompt_budget_chars": 24000,
  "provider_request_mode": "structured-native-tools",
  "sections": {
    "evidence": {
      "raw_chars": 2400,
      "rendered_chars": 2400
    },
    "history": {
      "raw_chars": 38009,
      "rendered_chars": 6900
    }
  },
  "history": {
    "collapsed_duplicate_reads": 2,
    "reused_file_summary_count": 6,
    "summarized_tool_count": 3
  },
  "evidence": {
    "file_count": 6
  }
}
```

关键指标解释：

| 指标 | 含义 |
|---|---|
| `provider_request_mode` | `structured-native-tools` 或 `text-compatibility` |
| `sections.*.raw_chars` | 该分区压缩前大小 |
| `sections.*.rendered_chars` | 实际进入兼容 Prompt 的大小 |
| `budget_reductions` | 哪个分区因局部或总预算被压缩 |
| `collapsed_duplicate_reads` | 被折叠的旧重复读取 |
| `reused_file_summary_count` | History 压缩时复用的文件摘要数 |
| `evidence.file_count` | 当前 Source Evidence 中的文件数 |

Trace 新增或明确使用：

```text
model_decision
tool_executed
tool_cache_hit
final_only_mode_entered
prompt_built
```

Session History 仍然最适合直观看问答和工具内容；Trace 则用于机器可读的阶段、耗时、缓存和停止原因，两者职责不再互相替代。

---

## 12. 测试覆盖与结论

本次新增/更新的关键回归测试包括：

1. `test_read_files_creates_per_file_structural_evidence_that_survives_history_clipping`
   - 构造两个大 Python 文件；
   - 使用 `read_files`；
   - 再填充大量 History；
   - 验证两个文件 Evidence 仍同时出现在 Prompt。

2. `test_unchanged_read_uses_compact_cache_and_file_change_invalidates_it`
   - 第一次真实读取；
   - 第二次命中缓存；
   - 修改文件；
   - 第三次自动重新读取并更新摘要。

3. `test_deepseek_request_separates_system_rules_and_role_aware_tool_messages`
   - 捕获实际 HTTP payload；
   - 验证第一条是 `system`；
   - 验证 assistant tool call 与 tool result 保持正确角色。

4. `test_native_history_budget_never_splits_assistant_tool_pair`
   - 在极小预算下验证 assistant/tool 消息不会被拆开。

5. `test_tool_budget_exhaustion_gets_one_final_only_recovery_decision`
   - 用完工具额度后仍能根据已有证据生成最终答复。

6. `test_interleaved_large_reads_reuse_evidence_instead_of_reaching_step_limit`
   - 复刻真实故障轨迹；
   - 两次交替重复读取命中缓存；
   - 最终有效工具步数只有 3；
   - Run 正常完成，不再触发 step limit。

最终全量测试结果：

```text
167 passed
```

## 13. 最终评价

修改前的压缩单位主要是“历史消息字符串”，核心假设是“最近内容最重要”。这个假设对普通聊天尚可，但对 Coding Agent 不成立：一次较早的 `read_file` 可能是最终结论最重要的证据。

修改后的压缩单位变为“信息职责”：

```text
规则用 Prefix 保护；
源代码事实用 Source Evidence 保底；
任务进度用 ProgressTracker 表达；
运行计数用最小 Run 视图表达；
最近原文用 History 补充；
完整回放仍留在 Session/Archive。
```

因此这次提升并不是单纯增加上下文窗口，而是让同样的上下文预算装入更稳定、更有决策价值的信息。对原始失败场景，系统已经从“6 次工具后仍无最终答案”变为“3 次有效工具动作完成，并且重复读取不再消耗工具额度”。

## 14. 工具输出截断改为保留头尾（2026-10-30）

**问题**：工具结果原先在两处都只保留开头：

| 位置 | 上限 | 原行为 |
|---|---|---|
| `ToolExecutor`（工具层） | 12000 字符 | `clip()` 只留开头 |
| `ContextManager` 原生 tool 消息 / 旧格式观察 / 文本 history | 2600 / 900 / 按行上限 | `_tail_clip()` 只留开头（名字叫 tail，实际保留的是头） |

pytest 的 `FAILED ... AssertionError` 和汇总行、编译器最后的报错都在**结尾**，
恰好被截掉，模型看不到测试为什么失败，只能猜。

**改法**：`workspace.clip_head_tail()` 头尾各留一半，中间插入
`...[omitted N chars from the middle]...`，换行原样保留。

- 标记长度计入 limit，调用方的分区预算不会被撑破；上限太小时退回只截开头。
- 只替换**工具结果**这几处；最终答案、prompt 分区等其它截断保持原样。
- `read_files` 的逐文件截断仍只保留开头（文件读取有 start/end，可按行号重读）。

回归测试：`tests/test_tool_output_clipping.py`（结尾失败行必须出现在工具结果和原生 tool 消息里，且长度不超限）。

## 15. 原生消息改为「稳定 → 只追加 → 易变」（2026-10-30）

**问题**：`provider_request()` 组装原生 tool calling 请求时：

1. 历史只取 Session **最后 10 条**、总共 **7200 字符**，单条工具结果 2600 字符、文本 1600 字符。
   模型改 B 文件时，A 文件的读取结果早已被挤出视野（多文件一致性、重构兼容任务失败正是这个症状）。
2. 每轮都变的 `Current task context` 放在**第一条**消息，后面整段历史每轮都命不中 prompt cache。

**改法**：

| 位置 | 改前 | 改后 |
|---|---|---|
| `system` | 规则前缀 | 规则前缀 + `long_term_memory` + `skills`（Session 级冻结快照） |
| 中间 | 最后 10 条，7200 字符 | 完整 Session 历史，按 `native_history_budget`（默认 200k 字符）从最旧淘汰，工具调用对不拆开 |
| 最后一条 | — | `working_memory` / `progress` / `evidence` + 完整当前请求 + 运行时指令 |

- 单条上限：工具结果 `NATIVE_TOOL_MESSAGE_LIMIT = MAX_TOOL_OUTPUT`（12000），文本 `NATIVE_TEXT_MESSAGE_LIMIT = 8000`。
- 历史最后一条是 user 时，易变上下文拼到它后面，避免连续两条 user 消息。
- 结果：上一轮请求去掉最后一条后，正好是这一轮请求的前缀，缓存可以连续命中。
- 文本协议 provider 走的 `build()` / `history_text()` 未改。

**已知限制（下一步 2b）**：超预算时仍是丢弃最旧轮次，没有摘要；而且一旦开始淘汰，每轮的起点都会移动，缓存会失效。
2b 计划改为接近窗口上限时**一次性**把旧轮次压缩成摘要，之后前缀重新稳定。200k 字符的默认值是保守估计，
DeepSeek V4 实际窗口待证实。

回归测试：`tests/test_native_history_window.py`（10 条以外的早期工具结果仍可见、上一轮请求是本轮前缀、
易变上下文在最后、按预算淘汰不拆工具对）。`tests/test_core_capacity.py` 中“当前请求在 `messages[0]`”的断言
随结构调整改为 `messages[-1]`。
