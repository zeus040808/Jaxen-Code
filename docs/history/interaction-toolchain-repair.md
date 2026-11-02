# HH Agent Pro 交互与工具调用链完整修复

## 1. 修复目标

本轮修复针对真实 Session `20260812-120316-468d6d` 和失败 Run
`run_20260812-120602-50c046` 暴露的问题，不只修复表面的 GBK 异常。

原始故障链是：

```text
用户要求总结 memory 目录
  ↓
模型没有稳定选择 list_files/read_file
  ↓
调用不存在的 list_dir，随后改用 run_shell
  ↓
Windows cmd.exe 收到 Bash 命令或 type UTF-8 文件
  ↓
subprocess 用 GBK 解码 UTF-8 字节，reader thread 崩溃
  ↓
result.stdout 变成 None，又触发 .strip() 异常
  ↓
模型继续重试同类 shell 调用
  ↓
防循环机制以 repeated_failed_call 停止任务
```

此外，Codex 多文件分析每读一个文件都需要一次新的 ephemeral `codex exec`，
真实 Trace 中单次模型决策曾耗时约 126～130 秒。旧设计需要 6～8 次模型往返，
造成“Agent 一直 Thinking、任务无法完成”的体验。

## 2. 修复后的执行结构

```text
用户请求
  ↓
ContextManager
  ├─ 不可裁剪：runtime identity
  ├─ 不可裁剪：核心行为规则
  ├─ 不可裁剪：完整工具目录
  ├─ 可压缩：Git / 项目文档快照
  ├─ 分层记忆与任务状态
  └─ 最近 12 条高保真历史
  ↓
Provider
  ├─ Codex CLI：JSONL 决策事件
  └─ DeepSeek：SSE + 原生 function tools
  ↓
结构化模型决策
  ├─ runtime_info
  ├─ list_files
  ├─ read_file / read_files
  ├─ search
  ├─ 条件审批 run_shell
  └─ 其他 HH 工具
  ↓
ToolExecutor 校验、执行、脱敏、记录
  ↓
Session + RunState + trace + report
```

## 3. Windows shell 修复

### 3.1 不再让 Python 自动使用 GBK 解码

旧代码：

```python
subprocess.run(..., capture_output=True, text=True)
```

中文 Windows 会按系统首选编码解码输出。当命令输出 UTF-8 源码时，可能出现
`UnicodeDecodeError`，并使 `stdout` 变成 `None`。

新实现让 subprocess 返回原始字节，再按以下顺序安全解码：

1. `utf-8-sig`；
2. 系统首选编码；
3. Windows `mbcs` / `gb18030`；
4. 最终以 UTF-8 `errors="replace"` 兜底。

`run_shell` 和基于 `rg` 的 `search` 共用这套逻辑。即使输出含中文或编码混合，
工具也不会因为解码异常把整个 Agent Loop 弄崩。

### 3.2 明确 shell 语义

Runtime identity 现在明确注入：

```text
platform: Windows ...
shell: cmd.exe
cwd: ...
repo_root: ...
```

提示词同时声明：Windows 下不能生成 `pwd`、`ls`、`cat`、`~`、`/dev/null`
等 Bash 专用命令；每次 shell 都从 `repo_root` 启动，某一次调用里的 `cd` 不会
改变后续调用的工作目录。

### 3.3 只读 shell 条件审批

`run_shell` 仍然是一个高能力工具，但审批由实际命令决定：

- `dir`、`type`、`git status/log/diff/show`、`rg`、`Get-ChildItem`、
  `Get-Content` 等明确检查命令可以只读执行；
- 重定向、删除、移动、复制、安装、Git 写操作等仍然必须审批；
- `echo` 仍需审批，因为它可以与重定向组合写文件；
- 未知命令按危险命令处理，不会因为分类器没见过就默认放行。

这减少了纯读取任务里无意义的连续 `[y/N]`，但没有扩大写权限。

## 4. 工具选择与批量读取

### 4.1 工具优先级变成显式契约

模型现在被明确要求：

- 列目录优先 `list_files`；
- 读取单文件优先 `read_file`；
- 比较或总结多个已知文件优先 `read_files`；
- 搜索代码优先 `search`；
- 只有没有专用工具时才使用 `run_shell`。

因此“总结目录源码”不再退化成 `dir + type/cat`。

### 4.2 新增 `read_files`

`read_files` 一次最多读取 8 个工作区内 UTF-8 文件，并有以下边界：

- 每个路径仍经过 workspace boundary；
- 禁止重复路径；
- 单次每文件最多读取 500 行；
- 总输出仍有预算，每个文件获得公平额度，后面的文件不会被第一个大文件完全挤掉；
- 工具为只读，不需要审批；
- 所有路径会进入 working memory 的 `recent_files`。

原任务以前通常需要：

```text
list → read A → read B → read C → read D → read E → final
```

现在通常变成：

```text
list → read_files([A, B, C, D, E]) → final
```

这对 Codex CLI 特别重要，因为它把 6～8 次高延迟模型往返压到 2～3 次。

### 4.3 常见别名纠正

模型偶尔会把 `list_files` 写成 `list_dir`。执行层现在将少量无歧义别名规范化：

```text
list_dir → list_files
read_multiple_files → read_files
get_runtime_info → runtime_info
```

这里只接受明确的一对一别名，不做模糊字符串猜测，避免错执行其他工具。

## 5. 安全的运行时自省

新增 `runtime_info`，只返回经过白名单筛选的信息：

```text
provider
model
platform
shell
cwd
repo_root
approval_policy
read_only
```

所以用户问“你现在是什么模型”时，Agent 不需要读取 `.env`。这避免 API Key
进入工具结果、Session、Trace 或模型上下文。

工具结果在进入 UI、TaskState、Session、Trace 和下一轮 prompt 之前统一经过
`redact_text()`；即便某个 shell 命令意外输出已配置的密钥值，也会被替换为
`<redacted>`。

## 6. 关键提示词不再被硬截断

旧实现把约 9.5～12 KB 的 prefix 从头截到 3.6 KB。真实边界位于
`run_shell` 工具描述中间，后续工具和整个 Workspace 信息都被删除。

新 prefix 明确拆成两部分：

```text
不可裁剪区
  ├─ Runtime identity
  ├─ 核心规则
  └─ 完整工具目录

Workspace snapshot (compressible)
  └─ Git 状态、提交和项目文档摘录
```

`ContextManager` 会动态计算不可裁剪区的实际长度作为 prefix floor。即使调用者
把 `prefix` 预算配置成 1000 字符，关键契约仍会完整保留；允许少量预算溢出，
也不允许静默删掉工具或运行环境。

默认总 prompt 预算由 12,000 字符调整为 24,000 字符，history 预算提高到
12,000 字符，最近高保真窗口由 6 条增加到 12 条。这样一次批量读取的多个源码
证据能保留到综合回答阶段，而不是立即压成前三行。

## 7. DeepSeek 原生工具调用

### 7.0 DeepSeek V4 Pro thinking 工具轮次的协议状态

DeepSeek V4 Pro 在 thinking 模式下发起工具调用时，SSE 返回的 assistant 消息除了
`tool_calls`，还包含 `reasoning_content`。下一次发送工具结果时，API 要求把这段
`reasoning_content` 放回同一条 assistant 工具消息；如果丢失，会直接返回 HTTP 400：

```text
The `reasoning_content` in the thinking mode must be passed back to the API.
```

当前实现会在 Provider 层完整收集分片，但不会打印内容：

```python
reasoning_parts = []

reasoning = delta.get("reasoning_content")
if isinstance(reasoning, str) and reasoning:
    reasoning_parts.append(reasoning)

self.last_native_assistant_message = {
    "tool_call_id": call["id"],
    "content": body_text or None,
    "reasoning_content": "".join(reasoning_parts),
}
```

AgentLoop 把 Provider 原始 `tool_call_id`、工具调用和 reasoning 协议状态保存在同一个
assistant history item；`ContextManager._native_history_messages()` 在下一轮恢复为：

```json
{
  "role": "assistant",
  "content": null,
  "reasoning_content": "完整原值",
  "tool_calls": [{"id": "call_ds_42", "...": "..."}]
}
```

随后的 tool message 使用同一个 `call_ds_42`。`reasoning_content` 只为满足 Provider
协议而持久化，不进入终端 UI、Trace、TaskState、Evidence 或普通 history 文本渲染。

旧 Session 已经丢失的 reasoning 无法安全伪造。对于 DeepSeek V4 Pro，这类旧 assistant
tool-call 不再作为原生工具消息重放；对应结果降级成 `Legacy tool observation` 普通上下文，
避免恢复旧会话时再次触发 HTTP 400。

DeepSeek Chat Completions 请求现在携带由 HH 工具注册表生成的 JSON Schema：

```json
{
  "type": "function",
  "function": {
    "name": "read_files",
    "parameters": {
      "type": "object",
      "properties": {
        "paths": {
          "type": "array",
          "items": {"type": "string"}
        }
      }
    }
  }
}
```

SSE 中分片到达的 `tool_calls[].function.arguments` 会先完整拼接，再做 JSON 解析，
最后转换成 HH 内部统一的 `<tool>...</tool>` 决策。执行仍然只能经过 HH 的
`ToolExecutor`，provider 无法绕过审批或路径校验。

这比要求 DeepSeek纯文本拼出 XML/JSON 更稳定，显著减少：

- 未知工具；
- `args={}`；
- JSON 半截；
- 标签格式错误。

隐藏 chain-of-thought 仍不会输出。`reasoning_content` 只转成“reasoning in
progress”状态，不把逐字私有推理打印或持久化。

## 8. 中断、停止和 Trace

### 8.1 Ctrl+C 正常收尾

旧行为在等待 Codex JSONL 时按 `Ctrl+C`，RunState 会永久停留在：

```text
status: running
finished_at: ""
```

新行为：

1. Codex adapter 捕获任意主线程中断并杀掉子进程；
2. Agent Loop 将 RunState 标记为 `stopped / interrupted`；
3. 写入 checkpoint、trace、report 和 `finished_at`；
4. one-shot 返回退出码 130；
5. REPL 打印已保存提示并继续可用。

### 8.2 Trace 补全

Trace 新增：

- `provider_event`：turn start/completed、reconnect、安全推理摘要；
- `model_decision`：`kind`、规范化工具名、脱敏参数或最终文本摘要；
- `run_interrupted`：中断发生时的 attempt 和 tool step；
- 原有 `tool_executed`、checkpoint 和 `run_finished` 保留。

这样复盘一次任务时，不再必须只依赖终端截图才能知道网络重连和模型到底选择了
哪个工具。

## 9. Codex CLI Session 复用取舍

本机 Codex CLI 提供 `codex exec resume`，但恢复要求先前 Session 已经持久化；
HH 当前使用 `--ephemeral`，目的是不把每轮包含 Session/history/memory 的完整内部
prompt 再长期保存到 Codex 的本地 Session 存储。

因此本轮没有为了速度取消 `--ephemeral`。优化采用更小的风险面：

- 保持 Codex read-only + ephemeral；
- 使用 `read_files` 降低模型往返次数；
- 提高一次工具证据的保真度；
- Trace 显示 reconnect，避免用户误以为 Agent 已死。

Codex CLI 参数与 `exec resume` 行为参考 OpenAI 官方 CLI 文档：
<https://developers.openai.com/codex/cli/reference>。

## 10. 测试结论

新增 `tests/test_interaction_repair.py`，覆盖：

1. 极小 prefix 预算下完整保留 runtime identity 和全部关键工具；
2. `runtime_info` 不输出密钥；
3. `list_dir` 安全映射到 `list_files`；
4. `read_files` 一次读取多个中文 UTF-8 文件；
5. Windows shell 中文输出不会触发 GBK 崩溃；
6. 只读命令分类严格，不放行重定向和修改命令；
7. 安全任务工具不被条件审批误伤；
8. DeepSeek 原生 tool call 的 SSE 参数分片能正确重组；
9. provider 状态和模型决策写入 Trace；
10. `KeyboardInterrupt` 会生成完整终止工件。

离线端到端复现还模拟了原故障：模型先返回旧的 `list_dir`，然后批量读取两个
中文源码文件。修复后结果为：

```text
status: completed
tool_steps: 2
attempts: 3
actual tools: list_files, read_files
```

项目全量结果：

```text
161 passed
```

另有一个工作区原有的 `.pytest_cache` WinError 5 警告，只影响 pytest 缓存写入，
不影响测试和 Agent 功能。

## 11. 手动复测

启动：

```powershell
cd C:\Users\ASUS\OneDrive\Desktop\HundredHouses-Agent
.\.venv\Scripts\python.exe -m hh_agent_pro --provider deepseek --approval ask
```

依次输入：

```text
你现在使用的 provider、模型和 shell 是什么？
列出 hh_agent_pro/memory 下的文件，不要使用 shell。
批量读取 hh_agent_pro/memory 下所有 Python 文件，总结每个文件职责。
比较 hh_agent_pro/features/memory.py 和 hh_agent_pro/memory 目录，判断是否应该合并。
```

预期：

- 第一问直接依据 runtime identity 或调用 `runtime_info`；
- 第二问使用 `list_files`，不请求审批；
- 第三问主要使用一次 `read_files`，不使用 `type/cat`；
- 不出现 GBK traceback；
- 最后一问能基于多个文件的实际代码给出综合结论；
- 中途按 `Ctrl+C` 后，最新 run 的状态为 `interrupted`，不是 `running`。

## 12. 仍然存在的外部限制

本轮修复保证的是 HH 自己的控制链、工具链和可观测性稳定，不可能消除 provider
网络本身的超时或限流。Codex 如果继续反复 reconnect，单次模型决策仍可能慢；
区别是现在任务需要的决策次数更少、重连会进入 Trace、用户中断后也能正确恢复。

## 13. 2026-08-12：回答截断与 DSML 协议泄漏修复

### 13.1 真实故障

手动测试暴露了两个新的完成阶段缺陷：

1. DeepSeek 回答恰好使用完 `512` 个输出 token，内容停在“仅当前”，但 Run
   仍被标记为 `completed / final_answer_returned`；
2. 工具额度用尽进入 final-only 后，DeepSeek 把内部 DSML 工具协议写入普通
   `content`，宽松 Parser 又把任意非空文本当成 final，最终把内部协议打印给用户。

这两项都不是终端渲染或上下文压缩故障。根因位于 Provider 完成元数据、Agent
控制循环和最终答案校验之间的边界。

### 13.2 Provider：保留完成原因

`OpenAIChatCompatibleModelClient.complete_stream()` 现在从每个 SSE choice 读取：

```python
choice = choices[0]
if choice.get("finish_reason") is not None:
    finish_reason = str(choice["finish_reason"])
```

并通过 `last_completion_metadata` 返回：

```python
{
    "finish_reason": finish_reason,
    "output_truncated": finish_reason == "length",
    "max_output_tokens_requested": int(max_new_tokens),
}
```

适配器只报告 Provider 事实，不在这里决定 Run 成功或失败。默认输出预算同时从
`512` 提高到 `2048`，降低普通长回答触顶概率；命令行仍可通过
`--max-new-tokens` 覆盖。

### 13.3 AgentLoop：一次自动续写，二次截断安全暂停

主循环在 Parser 之前检查 `output_truncated`。第一次截断时：

1. 提取并保存部分草稿到 `RunState.partial_answer`；
2. 进入 `output_recovery_active + final_only`；
3. 禁止工具，将草稿和“只返回缺失续文”的指令注入下一次模型决策；
4. 得到续文后与草稿合并，最后才标记 completed。

关键控制代码：

```python
if completion_metadata.get("output_truncated"):
    kind, payload = "truncated", raw

if kind == "truncated":
    partial = self._merge_continuation(agent.output_recovery_draft, partial_piece)
    run_state.partial_answer = partial
    agent.output_recovery_active = True
    agent.final_only_mode = True
    continue
```

如果自动续写仍然返回 `finish_reason=length`，Run 会明确停止为：

```text
status: stopped
stop_reason: output_truncated
```

不会伪装成成功。完整部分草稿同时持久化到：

- 当前 Run 的 `run_state.json -> partial_answer`；
- Session 的 `pending_output.partial_answer`。

用户下一轮输入“继续”“接着说”“continue”等精确续写指令时，Agent 会从
`pending_output` 恢复草稿、继续生成并清空 pending 状态，而不是重新调查项目。

### 13.4 Parser：拒绝内部协议和残缺标签

`CodingAgent.parse()` 新增最终答案入口校验：

- 拒绝 DeepSeek DSML 工具协议；
- 拒绝缺少 `</tool>` 的工具标签；
- 拒绝缺少 `</final>` 的最终答案；
- 同样检查 `needs_user_input` 和 `blocked` 闭合标签。

DSML 第一次出现时，AgentLoop 注入格式纠正并额外给一次模型决策机会。重复出现
则停止为 `model_protocol_error`，只向用户显示可理解的错误说明，原始控制协议不
会进入最终回答。

Trace 新增：

- `model_output_truncated`；
- `output_recovery_started`；
- `output_recovery_completed`；
- `internal_tool_protocol_rejected`。

### 13.5 新增测试与结论

新增回归覆盖：

1. DeepSeek SSE 的 `finish_reason=length` 不丢失；
2. 第一次截断自动续写并合并答案；
3. 第二次截断暂停并持久化部分草稿；
4. 下一轮“继续”恢复 Session 草稿；
5. DSML 首次泄漏被拦截并纠正；
6. DSML 重复泄漏安全停止且不标 completed；
7. 未闭合 `<final>` 不再误判成功；
8. CLI 默认输出预算为 2048。

最终全量测试：

```text
174 passed
```

仍有工作区原有的 `.pytest_cache` WinError 5 警告，只影响 pytest 缓存写入，不
影响测试结果或 Agent 行为。
