# HH Agent Pro 工具调用工程：从模型决策到安全执行

> 本文只讲当前项目 `hh_agent_pro` 的真实工具调用实现，不讲抽象的 Agent 理论。
>
> 主要对应源码：
>
> - `hh_agent_pro/tools.py`
> - `hh_agent_pro/tool_context.py`
> - `hh_agent_pro/tool_executor.py`
> - `hh_agent_pro/agent_loop.py`
> - `hh_agent_pro/runtime.py`
> - `hh_agent_pro/prompt_prefix.py`
> - `hh_agent_pro/context_manager.py`
> - `hh_agent_pro/providers/clients.py`
> - `hh_agent_pro/run_state.py`
>
> 阅读目标：即使你不懂 Python，也能讲清楚模型怎样看见工具、怎样申请调用、程序怎样校验和执行、结果怎样回到下一轮，以及整个系统怎样防止危险操作和死循环。

---

## 1. 先用一句话讲清整个设计

HH Agent Pro 的工具调用不是“大模型直接执行 Python 函数”，而是：

> 程序先把允许使用的工具菜单交给模型；模型只能提出一个结构化调用申请；AgentLoop 收到申请后交给 ToolExecutor；ToolExecutor 依次检查白名单、参数、工作区边界、重复调用和审批，再调用真正的 Python 函数；最后把成功或失败结果作为新的上下文交回模型，由模型决定下一步。

最关键的安全边界是：

> 模型有建议权，没有执行权；真正的执行权永远在本地 runtime 手里。

模型说“我要写文件”，不等于文件已经被写入。只有下面所有条件都通过后，写入才会发生：

1. 这个工具确实已注册；
2. 本轮允许使用它；
3. 参数合法；
4. 路径没有逃出项目目录；
5. 没有陷入重复调用；
6. 风险操作得到批准；
7. 底层函数执行成功。

---

## 2. 小学生也能听懂的比喻

把 Agent 想象成一家餐厅。

### 2.1 大模型是顾客

顾客可以看菜单，可以说：

> “我要一份 `read_file`，参数是 `README.md` 的第 1 到 80 行。”

但顾客不能自己进入厨房开火，也不能直接修改仓库文件。

### 2.2 工具注册表是菜单

菜单写着：

- 菜名，也就是工具名；
- 需要哪些材料，也就是参数；
- 这道菜做什么，也就是描述；
- 是否危险，也就是是否可能需要审批；
- 由哪位厨师制作，也就是真正的 Python 执行函数。

### 2.3 AgentLoop 是服务员

服务员负责不断循环：

1. 把菜单和当前情况告诉顾客；
2. 接收顾客这一轮的选择；
3. 如果顾客点工具，就把订单送到安全检查处；
4. 把厨房结果送回顾客；
5. 让顾客根据结果继续点菜或给出最终答案。

### 2.4 ToolExecutor 是安检员兼厨房总闸门

它检查：

- 菜单里有没有这道菜；
- 参数有没有写错；
- 是否想访问餐厅外面的文件；
- 是否连续做同一件没意义的事；
- 危险动作有没有获得批准。

检查通过后，它才允许真正的工具函数执行。

### 2.5 ToolContext 是厨师能拿到的受限钥匙包

工具函数不会拿到整个 HH Agent 对象，而只拿到自己可能需要的依赖，例如：

- 项目根目录；
- 安全路径解析器；
- 允许传给 shell 的环境变量；
- 任务存储；
- 记忆存储；
- Session 历史检索器。

这样工具不会天然拥有整个 Agent 的所有内部能力。

---

## 3. 只记住四个核心角色

| 角色 | 对应代码 | 只负责什么 |
|---|---|---|
| 工具注册表 | `tools.py` | 定义“有什么工具、参数是什么、由谁执行” |
| AgentLoop | `agent_loop.py` | 驱动“模型决策 → 工具执行 → 结果反馈”的循环 |
| ToolExecutor | `tool_executor.py` | 统一做校验、审批、执行、结果结构化和防循环 |
| ToolContext | `tool_context.py` | 给底层工具提供受限制的运行依赖 |

Provider 适配器、Session、RunState、Trace、Evidence 也会参与，但它们不是工具调用的四个核心角色：

- Provider 适配器负责翻译模型接口协议；
- Session 负责保存实际发生过的调用和结果；
- RunState 负责保存本次运行的计数和停止原因；
- Trace 负责工程诊断；
- Evidence 负责把读过的源码提炼成可复用证据；
- Checkpoint 负责中断恢复。

这条边界非常重要。不要再把所有“会保存信息的对象”都叫工具系统。

---

## 4. 一张图看懂完整主流程

```text
用户输入
   │
   ▼
AgentLoop 构建本轮上下文
   │
   ├── 系统规则
   ├── 当前任务和记忆
   ├── 最近历史
   └── 可用工具 schema
   │
   ▼
Provider 调用大模型
   │
   ├── 最终答案 ───────────────────────────────┐
   │                                           │
   └── 工具申请：name + args                    │
          │                                    │
          ▼                                    │
       AgentLoop 解析并记录 assistant tool_call │
          │                                    │
          ▼                                    │
       ToolExecutor                            │
          │                                    │
          ├── 是否在白名单                      │
          ├── 工具是否存在                      │
          ├── 参数和路径是否合法                │
          ├── 是否命中读取缓存                  │
          ├── 是否重复调用                      │
          ├── 是否需要审批                      │
          └── 执行真实 Python 函数               │
          │                                    │
          ▼                                    │
       ToolExecutionResult                     │
          │                                    │
          ├── 可读结果 content                  │
          └── 机器状态 metadata                 │
          │                                    │
          ▼                                    │
       保存为 role=tool 的 Session 事件          │
          │                                    │
          ▼                                    │
       下一轮重新交给模型 ── 继续调用或最终回答 ──┘
```

一句更短的背诵版：

> 注册工具，暴露菜单，模型申请，平台校验，本地执行，结果回填，再次决策。

---

## 5. 第一阶段：工具是怎样定义出来的

### 5.1 一个工具有四部分

以 `read_file` 为例，`tools.py` 中的定义可以简化成：

```python
BASE_TOOL_SPECS = {
    "read_file": {
        "schema": {
            "path": "str",
            "start": "int=1",
            "end": "int=200",
        },
        "risky": False,
        "description": "Read one UTF-8 workspace file.",
    },
}
```

逐项解释：

- `read_file`：工具名字，模型调用时必须使用它；
- `schema`：参数说明；
- `path: str`：必须提供一个字符串路径；
- `start: int=1`：可选整数，默认从第 1 行开始；
- `end: int=200`：可选整数，默认读到第 200 行；
- `risky: False`：普通读取不需要危险操作审批；
- `description`：告诉模型“什么时候应该用它”。

注意，这里还没有真正读取文件。它只是菜单说明。

### 5.2 工具说明和真正执行函数是分开的

`tools.py` 底部有一个分发表：

```python
_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "read_files": tool_read_files,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "runtime_info": tool_runtime_info,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    # 任务、定时任务、记忆工具……
}
```

这表达的是：

```text
工具名 read_file  →  Python 函数 tool_read_file
工具名 write_file →  Python 函数 tool_write_file
```

这样做比在 AgentLoop 里写一长串 `if name == ...` 更清楚，也更容易审查。

### 5.3 为什么说明和执行要分开

因为它们服务的对象不同：

- `schema` 和 `description` 是给模型看的；
- `risky` 是给安全层看的；
- `run` 函数是给本地程序执行的。

模型永远只看工具契约，不会得到 Python 函数对象。

---

## 6. 当前项目到底有哪些工具

当前合法工具一共 23 个，分为五组。

### 6.1 基础工作区工具：8 个

| 工具 | 作用 | 风险策略 |
|---|---|---|
| `list_files` | 列出目录和文件 | 安全 |
| `read_file` | 读取一个文本文件 | 安全 |
| `read_files` | 一次读取最多 8 个文件 | 安全 |
| `search` | 在工作区搜索文本 | 安全 |
| `run_shell` | 执行 shell 命令 | 按命令内容判断 |
| `runtime_info` | 查看脱敏运行环境 | 安全 |
| `write_file` | 写入完整文件 | 需要审批 |
| `patch_file` | 精确替换一段文本 | 需要审批 |

### 6.2 任务工具：7 个

`create_task`、`list_tasks`、`claim_task`、`complete_task`、`fail_task`、`block_task`、`record_evidence`。

它们修改的是 HH 自己的任务控制状态，不是直接修改用户源码，因此当前配置为不需要危险操作审批。

### 6.3 定时任务工具：3 个

`schedule_cron`、`list_crons`、`cancel_cron`。

它们只暴露给主 Agent。

### 6.4 持久记忆和历史检索工具：4 个

`memory_list`、`memory_update`、`session_search`、`session_read`。

其中 `memory_update` 会永久改变精选记忆，所以被标记为风险工具；其余是读取工具。

### 6.5 委派工具：1 个

`delegate` 用于让一个能力受限的只读子 Agent 做调查。只有当前深度小于最大深度时才会暴露。

---

## 7. 第二阶段：启动 HH 时怎样生成本轮工具注册表

`runtime.py` 初始化 HH 时做了两件关键事情：

```python
self.tools = self._apply_tool_allowlist(self.build_tools())
self.tool_executor = ToolExecutor(self)
```

可以读成：

1. 先根据当前身份和配置，生成可用工具；
2. 再应用本轮额外白名单；
3. 最后创建统一的工具执行器。

`build_tools()` 本身很短：

```python
def build_tools(self):
    return toolkit.build_tool_registry(self.tool_context())
```

它跳到 `tools.py` 的 `build_tool_registry()`：

```python
def build_tool_registry(context):
    tools = {
        name: {
            **spec,
            "run": partial(_TOOL_RUNNERS[name], context),
        }
        for name, spec in BASE_TOOL_SPECS.items()
    }

    tools.update({
        name: {
            **spec,
            "run": partial(_TOOL_RUNNERS[name], context),
        }
        for name, spec in TASK_TOOL_SPECS.items()
    })

    if context.depth == 0:
        # 主 Agent 才有定时任务和持久记忆工具
        ...

    if context.depth < context.max_depth:
        tools["delegate"] = {
            **DELEGATE_TOOL_SPEC,
            "run": partial(tool_delegate, context),
        }

    return tools
```

`partial()` 可以理解为“提前把 `context` 装进函数”。

原函数需要：

```python
tool_read_file(context, args)
```

绑定后，执行器只需要调用：

```python
tool["run"](args)
```

### 7.1 为什么不是自动扫描所有 Python 函数

当前系统使用显式注册，而不是动态发现。这是一项正确的安全设计：

- 模型只能看见明确允许的工具；
- 新写一个 Python 函数不会意外变成模型能力；
- 工具名、schema、风险和执行函数能一一审查；
- 子 Agent 的能力可以明确缩减。

### 7.2 `allowed_tools` 是第二层白名单

HH 构造参数还可以传入：

```python
allowed_tools=("list_files", "read_file", "search")
```

`_apply_tool_allowlist()` 会在完整注册表基础上再次过滤。因此：

> 注册表回答“系统理论上会什么”，allowlist 回答“本轮具体允许什么”。

---

## 8. ToolContext：为什么工具不直接拿整个 HH 对象

`tool_context.py` 的核心结构是：

```python
@dataclass
class ToolContext:
    root: Path
    path_resolver: Callable[[str], Path]
    shell_env_provider: Callable[[], dict]
    runtime_info_provider: Callable[[], dict]
    depth: int
    max_depth: int
    spawn_delegate: Callable[[dict], str]
    task_store: Any = None
    task_state: Any = None
    background_tasks: Any = None
    cron_scheduler: Any = None
    curated_memory: Any = None
    session_archive: Any = None
    session_id: str = ""
```

它像一个受限的依赖包。

例如读取文件只需要安全路径解析器，不需要知道模型 API Key；历史搜索只需要 `session_archive`，不需要控制终端 UI。

Runtime 负责组装它：

```python
def tool_context(self):
    return ToolContext(
        root=self.root,
        path_resolver=self.path,
        shell_env_provider=self.shell_env,
        runtime_info_provider=self.safe_runtime_info,
        depth=self.depth,
        max_depth=self.max_depth,
        spawn_delegate=self.spawn_delegate,
        task_store=self.project_tasks,
        task_state=self.task_state,
        background_tasks=self.background_tasks,
        cron_scheduler=self.cron_scheduler,
        curated_memory=self.curated_memory,
        session_archive=self.session_archive,
        session_id=str(self.session["id"]),
    )
```

设计目的不是为了“多一层显得高级”，而是为了明确依赖边界和方便测试。

---

## 9. 第三阶段：模型是怎样看见工具的

这里必须区分两类模型后端。

### 9.1 原生工具调用后端

当前 DeepSeek 的 OpenAI-compatible 客户端设置：

```python
self.supports_native_tools = True
```

AgentLoop 发送请求时会传入：

```python
tools=toolkit.provider_tool_schemas(agent.tools)
```

`provider_tool_schemas()` 会把 HH 的简写 schema 转为 OpenAI 风格的 function schema：

```python
{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read one UTF-8 workspace file...",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}
```

客户端随后把它放进 HTTP 请求：

```python
if tools:
    payload["tools"] = list(tools)
    payload["tool_choice"] = "auto"
```

`tool_choice = auto` 表示模型可以选择：

- 调用一个工具；
- 或者直接回答。

程序没有强迫每一轮都必须用工具。

### 9.2 文本协议后端

Codex CLI 等当前没有接入 HH 原生 function schema 的适配器使用：

```python
supports_native_tools = False
```

此时工具名、参数和格式会写入 Prompt，模型返回类似：

```xml
<tool>{"name":"read_file","args":{"path":"README.md"}}</tool>
```

或者适合多行文本的 XML 形式：

```xml
<tool name="write_file" path="hello.py">
  <content>print("hello")</content>
</tool>
```

两条链路只有“模型接口格式”不同，进入 AgentLoop 以后都归一成：

```python
kind = "tool"
payload = {
    "name": "read_file",
    "args": {"path": "README.md"},
}
```

这叫协议适配：上游可以不同，下游控制逻辑保持一套。

---

## 10. Prompt 如何教模型正确使用工具

`prompt_prefix.py` 会生成稳定规则，关键内容包括：

```text
- Use tools instead of guessing about the workspace.
- Return exactly one tool call, one needs_user_input, one blocked, or one final.
- Prefer dedicated safe tools before run_shell.
- Do not repeat the same tool call with the same arguments.
- Tool results include status, error_code, retryable and recommended_action.
- Never retry when retryable is false.
```

这里要分清两个层次：

### 10.1 Prompt 规则是行为引导

它告诉模型“应该怎样做”，能减少错误调用。

### 10.2 ToolExecutor 是强制控制

模型即使不听 Prompt，执行器仍会拒绝越界路径、非法参数或未批准的写操作。

因此：

> Prompt 不是安全边界。安全边界必须写在确定性的程序代码里。

### 10.3 原生工具为什么不在 Prompt 里重复 23 份 schema

`build_prompt_prefix()` 判断 `native_tools=True` 时，只保留行为策略：

```python
tool_text = (
    "Tool names and argument schemas are supplied through "
    "the provider's native tools field."
    if native_tools
    else "完整工具文本列表"
)
```

因为 DeepSeek 已经从 API 的 `tools` 字段获得精确 schema，再在系统 Prompt 中完整复制一遍会浪费上下文。

---

## 11. 第四阶段：AgentLoop 发起一次模型决策

主循环位于 `AgentLoop.run()`：

```python
tool_steps = 0
attempts = 0
max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)

while attempts < max_attempts:
    attempts += 1
    run_state.record_attempt()

    prompt, prompt_metadata = agent._build_prompt_and_metadata(
        user_message
    )

    raw = agent.model_client.complete_stream(
        prompt,
        agent.max_new_tokens,
        tools=(
            None
            if agent.final_only_mode
            else toolkit.provider_tool_schemas(agent.tools)
        ),
        **provider_kwargs,
    )

    kind, payload = agent.parse(raw)
```

每次循环可以理解为一次“模型决策”。

模型每一轮只应做一件事：

1. 申请一个工具；或
2. 给最终答案；或
3. 请求用户补充；或
4. 说明被阻塞。

模型调用工具后不会在同一个响应里继续假装知道执行结果，因为当时工具还没有执行。

---

## 12. DeepSeek 的流式工具调用怎样被拼回来

DeepSeek 通过 SSE 流式返回数据。一次工具调用可能被拆成很多小片段：

```text
第 1 段：工具名 read_files
第 2 段：参数开头 {"paths":["a.py",
第 3 段：参数结尾 "b.py"]}
```

`providers/clients.py` 会按 `index` 收集：

```python
tool_calls = {}

for tool_delta in delta.get("tool_calls") or []:
    index = int(tool_delta.get("index", 0))
    call = tool_calls.setdefault(
        index,
        {"id": "", "name": "", "arguments": []},
    )

    if tool_delta.get("id"):
        call["id"] = str(tool_delta["id"])

    function = tool_delta.get("function") or {}
    if function.get("name"):
        call["name"] = str(function["name"])
    if function.get("arguments"):
        call["arguments"].append(str(function["arguments"]))
```

流结束后再解析完整 JSON：

```python
arguments = json.loads("".join(call["arguments"]) or "{}")
```

然后转换成 HH 内部统一格式：

```python
return "<tool>" + json.dumps({
    "name": call["name"],
    "args": arguments,
}) + "</tool>"
```

这样 AgentLoop 无须为每个 Provider 写一套工具执行分支。

### 12.1 当前只执行一个工具调用

Prompt 明确要求每次只返回一个工具调用。DeepSeek 适配器收到多个 `tool_calls` 时，目前选择索引最小的第一个：

```python
call = tool_calls[min(tool_calls)]
```

这意味着当前系统是严格的“每轮一个动作”模式，不是并行工具调用模式。

优点是控制流简单、历史配对清楚、审批容易理解；缺点是比较多个互不依赖文件时轮数可能更多，所以项目提供了 `read_files` 批量读取工具。

---

## 13. 文本工具调用怎样解析

Provider 返回文本后，`runtime.py` 的 `HH.parse(raw)` 将它转为控制动作：

```python
if "<tool>" in raw:
    body = HH.extract(raw, "tool")
    payload = json.loads(body)

    if not isinstance(payload, dict):
        return "retry", "格式错误"

    if not str(payload.get("name", "")).strip():
        return "retry", "缺少工具名"

    args = payload.get("args", {})
    if not isinstance(args, dict):
        return "retry", "参数必须是对象"

    return "tool", payload
```

如果 JSON 不完整、工具名缺失或 `args` 不是字典，系统不会执行，而是进入一次格式恢复。

解析层只回答：

> “这段模型输出看起来是不是一个结构正确的工具申请？”

它还没有验证路径是否存在，也没有检查审批。这些属于 ToolExecutor。

---

## 14. 第五阶段：AgentLoop 接住工具申请

当 `kind == "tool"` 时，主循环做以下事情：

```python
name = agent.canonical_tool_name(payload.get("name", ""))
args = payload.get("args", {})

agent.emit_ui("tool_started", name=name, args=args)

tool_call_id = provider_call_id or generate_call_id()

agent.record({
    "role": "assistant",
    "content": "",
    "tool_call": {
        "id": tool_call_id,
        "name": name,
        "args": args,
    },
})

tool_result = agent.execute_tool(name, args)
```

这里有四个关键动作。

### 14.1 统一别名

例如旧名字：

```text
list_dir            → list_files
read_multiple_files → read_files
get_runtime_info    → runtime_info
```

先转换成统一名字，后续校验、计数和历史记录不会被别名弄乱。

### 14.2 先记录“模型申请了什么”

Session 中写入一个 `role=assistant` 的 `tool_call` 事件。

### 14.3 生成或保留 `tool_call_id`

它是一次工具申请的订单号，用于把后面的工具结果与这次申请配对。

### 14.4 统一进入 `execute_tool()`

AgentLoop 不直接调用 `tool_read_file()`。所有工具都必须经过 ToolExecutor 总闸门。

---

## 15. ToolExecutor：真正的工具总闸门

Runtime 的入口很薄：

```python
def execute_tool(self, name, args):
    name = self.canonical_tool_name(name)
    result = self.tool_executor.execute(name, args)

    redacted_content = self.redact_text(result.content)
    ...

    self.task_state.observe_tool(
        name, args, result.content, result.metadata
    )
    self.session_path = self.session_store.save(self.session)
    return result
```

真正的总闸门在 `tool_executor.py`：

```python
def execute(self, name, args):
    name = self.agent.canonical_tool_name(name)
    result = self._execute(name, args)
    return self._observe_result(name, args, result)
```

可以把它分成两段：

- `_execute()`：做一次调用的校验和执行；
- `_observe_result()`：观察失败是否重复，决定是否阻止死循环。

---

## 16. ToolExecutor 的检查顺序

执行顺序很重要，当前顺序是：

```text
1. 本轮 allowlist 是否允许
2. 注册表中是否存在
3. 参数和资源是否合法
4. 读取缓存是否命中
5. 是否为无意义重复调用
6. 是否需要审批，审批是否通过
7. 风险操作前拍工作区快照
8. 调用真实 Python 函数
9. 风险操作后拍快照并计算差异
10. 更新工作记忆、任务状态和过程摘要
11. 把结果转成统一结构
12. 检查相同失败是否重复
```

之所以先校验、后审批，是因为不能让用户为一个明显不存在或参数错误的调用反复确认。

---

## 17. 第一关：本轮是否允许这个工具

```python
if agent.allowed_tools is not None \
        and name not in agent.allowed_tools:
    return ToolExecutionResult(
        content=f"error: tool '{name}' is not allowed",
        metadata={
            "tool_status": "rejected",
            "tool_error_code": "tool_not_allowed",
            "retryable": False,
            "stop_run": True,
        },
    )
```

这类错误不是模型换一个写法就能解决，所以：

- `retryable=False`；
- `stop_run=True`；
- AgentLoop 安全停止并说明阻塞原因。

---

## 18. 第二关：工具是否真实存在

```python
tool = agent.tools.get(name)
if tool is None:
    return ToolExecutionResult(
        content=f"error: unknown tool '{name}'",
        metadata={
            "tool_status": "rejected",
            "tool_error_code": "unknown_tool",
            "retryable": True,
        },
    )
```

未知工具可能只是模型写错了名字，所以允许模型根据菜单纠正一次，而不是立刻结束整个运行。

---

## 19. 第三关：参数和资源校验

执行器调用：

```python
agent.validate_tool(name, args)
```

它再跳到：

```python
toolkit.validate_tool(self.tool_context(), name, args)
```

### 19.1 `read_file` 的校验

```python
if name == "read_file":
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")

    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
```

它检查：

- 必须有 `path`；
- 路径必须指向文件；
- 起止行数合理。

### 19.2 `read_files` 的校验

它额外要求：

- 至少 1 个路径；
- 最多 8 个路径；
- 路径不能重复；
- 每个路径都必须是文件；
- 一次最多读取 500 行范围。

### 19.3 `run_shell` 的校验

它要求：

- 命令不能为空；
- 超时时间必须在 1 到 120 秒之间。

### 19.4 为什么 Provider schema 后还要本地校验

因为模型接口 schema 只能降低格式错误概率，不能作为最终信任边界：

- 不同 Provider 可能对 schema 执行严格程度不同；
- 文本协议后端没有原生 JSON schema 保证；
- `path.is_file()` 这类实时资源事实无法只靠 JSON schema 判断；
- 安全规则必须由本地程序控制。

---

## 20. 最重要的安全检查：路径不能逃出工作区

所有文件路径最终都会经过：

```python
context.path(raw_path)
```

它调用 Runtime 提供的安全路径解析器 `HH.path()`。概念上相当于：

```python
candidate = (repo_root / raw_path).resolve()

if candidate 不在 repo_root 里面:
    raise ValueError("path escapes workspace")
```

例如项目根目录是：

```text
C:\work\my-project
```

模型申请：

```json
{"path": "../secret.txt"}
```

解析后会跑到项目外面，因此被标记为：

```text
tool_error_code = path_escape
security_event_type = path_escape
retryable = false
```

这不是普通的“文件不存在”，而是安全事件。

---

## 21. 第四关：读取缓存

模型很容易重复读取同一个没有变化的文件。当前系统不会再次执行完整读取，而是计算：

```text
缓存键 = 工具名 + 标准化参数 + 文件当前内容哈希
```

核心逻辑：

```python
cache_key = self._read_cache_key(name, args)

if cache_key and cache_key in self._read_cache:
    return ToolExecutionResult(
        content=(
            "cached observation: unchanged successful "
            "read reused; no tool step consumed."
        ),
        metadata={
            "tool_status": "ok",
            "cache_hit": True,
            "effective_tool_step": False,
        },
    )
```

它只对 `read_file` 和 `read_files` 生效。

### 21.1 文件变了怎么办

文件内容哈希会变化，缓存键也会变化，于是重新读取。缓存不是永远相信旧结果，而是带新鲜度检查。

### 21.2 为什么缓存调用不占工具额度

因为没有发生新的外部观察，只是提醒模型使用已经存在的证据：

```python
effective_tool_step = False
```

AgentLoop 看到它后不会增加 `tool_steps`。

### 21.3 为什么第二次缓存命中后进入 final-only

如果模型已经两次重复请求相同且未变化的读取，说明它可能陷入循环。AgentLoop 会关闭后续工具，要求它利用已有证据回答。

---

## 22. 第五关：重复调用防循环

对于非读取工具，系统检查 Session 中最近两个工具事件：

```python
def repeated_tool_call(self, name, args):
    tool_events = [
        item for item in self.session["history"]
        if item["role"] == "tool"
    ]

    if len(tool_events) < 2:
        return False

    recent = tool_events[-2:]
    return all(
        item["name"] == name and item["args"] == args
        for item in recent
    )
```

也就是说，同样的非读取调用连续出现两次后，第三次会被拒绝。

为什么读取工具不用这套简单规则？

因为文件可能在两次调用之间发生变化，所以读取工具使用“参数 + 文件哈希”的新鲜度缓存，更准确。

---

## 23. 第六关：风险判断和用户审批

### 23.1 普通文件工具

- `read_file`：不审批；
- `write_file`：审批；
- `patch_file`：审批；
- `memory_update`：审批。

### 23.2 `run_shell` 是条件审批

虽然 spec 中 `run_shell` 标记为风险工具，但代码会检查具体命令：

```python
def tool_requires_approval(name, args):
    if name == "run_shell":
        return not shell_command_is_read_only(
            args.get("command", "")
        )
```

明确的只读命令，例如：

```text
dir /b
type README.md
git status
git diff
rg pattern
```

可以不审批。

修改性命令、重定向或无法确认安全的命令，例如：

```text
del file.txt
git commit
pip install ...
type README.md > copy.txt
echo something
```

会进入审批。

这里采用的是保守白名单：不能明确确认只读，就按有风险处理。

### 23.3 三种审批策略

Runtime 的 `approve()`：

```python
def approve(self, name, args):
    if self.read_only:
        return False
    if self.approval_policy == "auto":
        return True
    if self.approval_policy == "never":
        return False

    self.emit_ui("approval_requested", name=name, args=args)
    answer = input("Allow this action? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}
```

含义：

| 配置 | 行为 |
|---|---|
| `read_only=True` | 所有风险动作都拒绝 |
| `approval_policy=auto` | 自动批准风险动作 |
| `approval_policy=never` | 永不批准风险动作 |
| `approval_policy=ask` | 在终端询问用户 |

`read_only` 的优先级最高。即使策略是 `auto`，只读模式也不能写。

---

## 24. 第七步：真正执行 Python 工具函数

所有关卡通过后，执行器调用：

```python
content = agent.redact_text(
    clip(tool["run"](args))
)
```

这一行做了三件事：

1. `tool["run"](args)`：执行真实函数；
2. `clip(...)`：限制超长输出；
3. `redact_text(...)`：把配置的密钥打码。

为什么要先裁剪和脱敏？因为工具结果后面会进入：

- 终端 UI；
- Session history；
- 下一轮模型上下文；
- Trace；
- 任务状态或证据。

工具输出是一个会流向多个地方的数据入口，必须尽早处理秘密和体积。

---

## 25. 风险操作前后为什么要拍工作区快照

执行器对需要审批的操作这样做：

```python
before_snapshot = agent.capture_workspace_snapshot()

content = tool["run"](args)

after_snapshot = agent.capture_workspace_snapshot()

affected_paths, diff_summary = (
    agent.diff_workspace_snapshots(
        before_snapshot,
        after_snapshot,
    )
)
```

它要回答两个问题：

1. 工具声称做了什么？
2. 工作区实际上改变了什么？

如果 shell 命令返回非零退出码，但已经改动了文件，就不能简单写成“完全失败”，而会标记：

```text
tool_status = partial_success
tool_error_code = tool_partial_success
workspace_changed = true
```

这样模型下一轮会先检查现场，而不是盲目重试同一个可能已经执行了一半的命令。

---

## 26. 工具结果为什么不是一段随便的字符串

统一结果对象是：

```python
@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict
```

### 26.1 `content` 给人和模型读

例如：

```text
# README.md
1: # HundredHouses Agent
2: ...
```

或者：

```text
error: invalid arguments for read_file: path is not a file
```

### 26.2 `metadata` 给程序判断

常见字段：

| 字段 | 含义 |
|---|---|
| `tool_status` | `ok`、`error`、`rejected` 或 `partial_success` |
| `tool_error_code` | 稳定的机器错误码 |
| `security_event_type` | 是否为越界、审批拒绝等安全事件 |
| `risk_level` | 风险等级 |
| `read_only` | 本次动作是否被当作只读 |
| `affected_paths` | 实际受影响的文件 |
| `workspace_changed` | 工作区是否变化 |
| `diff_summary` | 变化摘要 |
| `retryable` | 是否值得纠正后再试 |
| `recommended_action` | 建议模型下一步做什么 |
| `stop_run` | 是否立即停止本次运行 |

如果只有字符串“失败了”，模型不知道该纠正参数、换工具，还是彻底停止。结构化结果把控制语义说清楚。

---

## 27. 返回给模型的内容长什么样

`ToolExecutionResult.prompt_content()` 会生成：

```xml
<tool_result>
这里是工具的可读输出
</tool_result>
<tool_outcome>
{
  "status": "ok",
  "error_code": "",
  "retryable": false,
  "recommended_action": "",
  "failure_repeat_count": 0
}
</tool_outcome>
```

它同时给模型两种信息：

- `tool_result`：观察到了什么；
- `tool_outcome`：这次执行在控制层意味着什么。

模型被 Prompt 明确要求尊重 `retryable`。如果为 `false`，不能原样重试。

---

## 28. 工具调用和结果怎样写进 Session

一次完整工具交换会保存两个事件。

### 28.1 模型的工具申请

```json
{
  "role": "assistant",
  "content": "",
  "tool_call": {
    "id": "call_abc123",
    "name": "read_file",
    "args": {"path": "README.md"}
  }
}
```

### 28.2 本地工具结果

```json
{
  "role": "tool",
  "name": "read_file",
  "args": {"path": "README.md"},
  "content": "<tool_result>...</tool_result>...",
  "tool_call_id": "call_abc123"
}
```

两个相同的 ID 表示：

```text
申请 call_abc123  ↔  结果 call_abc123
```

这就是为什么只保存一段模糊的 `history` 文本还不够。原生 Provider 需要角色正确、ID 正确的工具调用对。

---

## 29. 第六阶段：工具结果怎样注入下一轮

AgentLoop 执行完后：

```python
agent.record({
    "role": "tool",
    "tool_call_id": tool_call_id,
    "content": tool_result.prompt_content(),
})

continue
```

`continue` 会回到主循环开头，再次构建上下文并调用模型。

对于 DeepSeek 原生工具协议，`ContextManager._native_history_messages()` 会恢复：

```python
{
    "role": "assistant",
    "tool_calls": [{
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": "{...}",
        },
    }],
}
```

紧接着：

```python
{
    "role": "tool",
    "tool_call_id": call_id,
    "content": "工具结果",
}
```

因此模型下一轮能准确理解：

> “我刚才申请读取 README，程序已经返回了这个结果。现在应该根据结果继续。”

### 29.1 最近历史不是无限注入

原生消息路径先查看最近 10 条 Session 事件，再在约 7200 字符预算内选择，而且不会故意拆开有效的工具申请—结果对。

旧事件仍保存在 Session 和 Archive 中，只是不会每一轮全部发给模型。

---

## 30. DeepSeek V4 的 `reasoning_content` 为什么必须保存

DeepSeek thinking mode 在返回工具调用时，还可能返回一个协议字段：

```json
{
  "reasoning_content": "...",
  "tool_calls": [...]
}
```

DeepSeek 要求下一轮回传同一次 assistant tool-call 时，连同原值一起回传。否则会出现：

```text
HTTP 400:
The reasoning_content in the thinking mode must be passed back
```

当前客户端会完整收集：

```python
self.last_native_assistant_message = {
    "tool_call_id": call["id"],
    "content": body_text or None,
    "reasoning_content": "".join(reasoning_parts),
}
```

AgentLoop 将它保存在对应的 assistant Session 事件中；下一轮 `ContextManager` 原样恢复。

### 30.1 它不是给用户看的“思考过程”

这只是 Provider 继续工具协议所需的私有状态：

- 不裁剪；
- 不总结；
- 不打印到 UI；
- 不写入 Trace 供用户阅读；
- 只在协议重放时使用。

终端中的：

```text
DeepSeek reasoning in progress
```

只是状态提示，不是模型完整思维链。

---

## 31. `attempts` 和 `tool_steps` 到底有什么区别

这是最容易看错的计数。

### 31.1 `attempts`

每请求模型一次就加一：

```python
attempts += 1
run_state.record_attempt()
```

它统计“模型做了几轮决策”。

### 31.2 `tool_steps`

每个非缓存工具申请进入 ToolExecutor 后通常加一：

```python
effective_step = tool_result.metadata.get(
    "effective_tool_step", True
)

if effective_step:
    tool_steps += 1
    run_state.record_tool(name)
```

必须精确理解：

> `tool_steps` 不是成功工具数，而是非缓存的有效工具尝试数。

因此下面通常都会占一次：

- 成功读取；
- 参数错误；
- 工具不存在；
- 审批拒绝；
- 工具执行报错。

只有命中新鲜度读取缓存时：

```text
effective_tool_step = false
```

不占工具额度。

### 31.3 为什么需要两个上限

默认：

```python
max_steps = 12
max_attempts = max(max_steps * 3, max_steps + 4)
```

所以默认大致是：

```text
最多 12 次非缓存工具尝试
最多 36 次模型决策
```

多出来的模型决策用于：

- 工具执行后的下一轮判断；
- 工具格式纠正；
- 输出截断恢复；
- 工具额度用完后的最终总结。

这正是旧设计中“模型轮数等于工具次数”导致频繁提前停止的修复方向。

---

## 32. 工具额度耗尽后为什么不是立刻粗暴停止

如果模型还想调用工具，但 `tool_steps >= max_steps`，系统第一次会：

```python
agent.final_only_mode = True
continue
```

下一轮：

- 不再向 Provider 发送工具 schema；
- 上下文加入权威指令：工具已禁用；
- 要求模型根据现有证据返回最终答案、请求补充或说明阻塞。

这叫 final-only 恢复。

为什么合理？

因为第 12 次工具可能刚好拿到关键结果。如果立即返回“超过步数”，模型根本没有机会把最后一份结果整理成人话。

只有模型在 final-only 模式仍坚持申请工具时，才以 step limit 停止。

---

## 33. 相同失败为什么第二次就停止

ToolExecutor 为失败生成指纹：

```text
失败指纹 = 工具名 + 标准化参数 + 错误码
```

`_observe_result()` 统计相同失败：

```python
count = self._failure_counts.get(fingerprint, 0) + 1

if count >= 2 and metadata["retryable"]:
    metadata.update({
        "tool_error_code": "repeated_failed_call",
        "retryable": False,
        "stop_run": True,
    })
```

例如模型连续两次执行：

```json
{
  "name": "read_file",
  "args": {"path": "missing.py"}
}
```

第一次返回“文件不存在，可以纠正路径”；第二次仍完全相同，就说明模型没有根据反馈调整。系统停止，防止继续浪费轮数。

注意：换成真正不同的路径或不同工具，不属于同一个失败指纹。

---

## 34. 什么错误会继续，什么错误会立刻停

| 情况 | `retryable` | `stop_run` | 结果 |
|---|---:|---:|---|
| 未知工具名 | true | false | 模型可选合法工具重试 |
| 普通参数错误 | true | false | 模型可纠正参数 |
| 普通工具执行失败 | true | false | 模型可检查后换方案 |
| 相同可重试失败第二次出现 | false | true | 防死循环，停止 |
| 本轮工具不在 allowlist | false | true | 能力不存在，停止 |
| 路径逃出工作区 | false | 后续安全阻塞 | 不允许原样重试 |
| 审批拒绝或只读阻止 | false | true | 停止并说明所需权限 |
| shell 失败但工作区已变化 | true | false | 标记 partial success，先检查现场 |

设计原则是：

> 只有“改变参数或方法有希望解决”的错误才允许重试；权限和安全边界不是靠多试几次解决的。

---

## 35. 工具调用后还会更新哪些附属系统

工具执行完成后，AgentLoop 和 Runtime 还会通知多个观察者：

```text
工具结果
  ├── Session：保存真实调用和结果
  ├── RunState：更新工具次数和 last_tool
  ├── Trace：记录耗时、状态、错误码和差异
  ├── TaskState：更新任务进展和失败
  ├── Evidence：记录读过哪些来源
  ├── Working memory：更新最近文件/过程笔记
  └── Checkpoint：保存可恢复位置
```

它们的作用不同。

### 35.1 Session 是权威事件流

回答“模型申请了什么、工具返回了什么”。

### 35.2 RunState 是当前运行仪表盘

回答“已经决策几轮、用了几个工具、为何停止”。它每次覆盖最新状态，不是历史日志。

### 35.3 Trace 是工程诊断日志

回答“每一步耗时多少、状态码是什么、是否命中缓存”。它适合机器诊断，不替代 Session 的人类复盘。

### 35.4 Evidence 是源代码证据卡片

读取源码后，提炼文件结构和新鲜度，避免后续只凭旧对话猜测。它不是工具结果本身，也不负责执行工具。

### 35.5 Checkpoint 是恢复书签

每次工具之后创建恢复点，让中断后的任务知道从哪里继续。

这些都是工具调用的“旁路观察者”，不是执行主链。删掉 Trace 不会让 `read_file` 无法执行，但会降低诊断能力；删掉 ToolExecutor 则会失去真正的安全闸门。

---

## 36. 完整例子一：读取并解释一个文件

用户说：

```text
请解释 README.md 的启动方法。
```

### 第 1 次模型决策

模型看见 `read_file` schema，返回：

```json
{
  "name": "read_file",
  "args": {
    "path": "README.md",
    "start": 1,
    "end": 120
  }
}
```

### AgentLoop 解析

得到：

```text
kind = tool
name = read_file
args = {...}
```

### ToolExecutor 检查

```text
allowlist：通过
工具存在：通过
路径在工作区：通过
文件存在：通过
行号合法：通过
缓存：第一次，无缓存
审批：只读，不需要
```

### 真正读取

`tool_read_file(context, args)` 返回带行号文本。

### 结果保存

Session 写入：

```text
assistant tool_call call_123
tool result call_123
```

`tool_steps` 从 0 变为 1。

### 第 2 次模型决策

模型收到刚才的 README 内容，返回最终解释。

最终统计：

```text
attempts = 2
tool_steps = 1
```

---

## 37. 完整例子二：修改文件并测试

用户说：

```text
把 hello.py 的返回值从 1 改成 2，然后运行测试。
```

可能的控制流：

```text
decision 1 → read_file hello.py
decision 2 → patch_file hello.py
               └── 需要审批
decision 3 → run_shell pytest -q
               └── 是否审批取决于命令分类
decision 4 → final
```

`patch_file` 前后会拍工作区快照，因此结果 metadata 可包含：

```json
{
  "tool_status": "ok",
  "workspace_changed": true,
  "affected_paths": ["hello.py"],
  "diff_summary": ["modified: hello.py"]
}
```

最终回答不能只依赖“模型觉得自己改了”，而应依赖工具结果和测试证据。

---

## 38. 完整例子三：审批被拒绝

用户要求执行写操作，但配置是：

```text
approval_policy = never
```

模型申请 `write_file` 后：

```text
ToolExecutor 判断需要审批
  ↓
approve() 返回 false
  ↓
tool_status = rejected
tool_error_code = approval_denied
retryable = false
stop_run = true
  ↓
AgentLoop 返回 Blocked safely
```

这时继续请求模型换一种语气再次写文件没有意义，所以立即停下是正确行为。

---

## 39. 完整例子四：错误参数后纠正

第一次模型申请：

```json
{
  "name": "read_file",
  "args": {"path": "missing.py"}
}
```

ToolExecutor 返回：

```text
status = rejected
error_code = invalid_arguments
retryable = true
recommended_action = correct the arguments
```

下一轮模型搜索文件：

```json
{
  "name": "search",
  "args": {
    "pattern": "目标函数名",
    "path": "."
  }
}
```

这是“有材料变化的重试”，允许继续。

如果模型第二次仍原样读取 `missing.py`，失败指纹重复，系统会停止防循环。

---

## 40. 从用户输入开始的函数跳转地图

这是阅读源码时最实用的一条路线：

```text
CLI 交互循环
  ↓
HH.ask(user_message)                         runtime.py
  ↓
AgentLoop(self).run(user_message)            agent_loop.py
  ↓
agent._build_prompt_and_metadata(...)        runtime/context_manager
  ↓
ContextManager.provider_request(...)         context_manager.py
  ↓
provider_tool_schemas(agent.tools)           tools.py
  ↓
model_client.complete_stream(...)            providers/clients.py
  ↓
HH.parse(raw)                                runtime.py
  ↓ kind == tool
agent.execute_tool(name, args)                runtime.py
  ↓
ToolExecutor.execute(name, args)              tool_executor.py
  ↓
ToolExecutor._execute(name, args)
  ├── agent.validate_tool(...)
  │     └── tools.validate_tool(...)
  ├── agent.tool_requires_approval(...)
  ├── agent.approve(...)
  └── tool["run"](args)
        └── tools.tool_read_file / tool_write_file / ...
  ↓
ToolExecutionResult
  ↓
AgentLoop 写入 role=tool 的 Session 事件
  ↓
continue 回到下一次模型决策
```

如果你能不看文档画出这条路线，就已经真正理解了工具调用主干。

---

## 41. 新增一个工具需要改哪些地方

假设要增加 `count_lines`。

### 第一步：增加 spec

```python
BASE_TOOL_SPECS["count_lines"] = {
    "schema": {"path": "str"},
    "risky": False,
    "description": "Count lines in one workspace text file.",
}
```

### 第二步：写真实函数

```python
def tool_count_lines(context, args):
    path = context.path(args["path"])
    text = path.read_text(encoding="utf-8")
    return f"line_count: {len(text.splitlines())}"
```

### 第三步：加入 runner 分发表

```python
_TOOL_RUNNERS["count_lines"] = tool_count_lines
```

### 第四步：加入参数校验

```python
if name == "count_lines":
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    return
```

### 第五步：补测试

至少测试：

- 正常文件；
- 文件不存在；
- `../outside.txt` 路径逃脱；
- schema 能正确发给原生 Provider；
- 工具结果能进入下一轮模型上下文。

不需要修改 AgentLoop。因为 AgentLoop 面向统一的 `name + args + ToolExecutionResult` 协议，而不关心某个具体工具的业务逻辑。

---

## 42. 当前设计做得好的地方

### 42.1 模型和执行权彻底分开

模型只能生成意图，ToolExecutor 掌握最终执行权。

### 42.2 工具能力显式注册

没有隐式暴露任意 Python 函数，能力范围清晰可审计。

### 42.3 Provider 差异被隔离

DeepSeek 原生 tool calls 和文本 `<tool>` 最终进入同一 AgentLoop。

### 42.4 失败是结构化状态

模型能区分参数错误、审批拒绝、路径越界、部分成功和重复失败。

### 42.5 工具额度和模型决策额度分开

执行最后一个工具后还有机会生成最终答案，不会在结果出来的一瞬间粗暴截断。

### 42.6 读取缓存带文件新鲜度

既避免重复消耗，又不会在文件变化后继续使用旧内容。

### 42.7 风险动作检查真实工作区差异

不只相信命令返回文本，还观察实际修改了哪些文件。

---

## 43. 当前设计仍然存在的局限

为了让你能独立讲解，也必须诚实说明不足。

### 43.1 `tools.py` 职责偏多

它同时包含：

- spec；
- schema 转换；
- 校验；
- shell 风险判断；
- 全部工具实现；
- runner 分发表。

工具继续增加时，文件会越来越大。未来可以按领域拆成 `workspace_tools.py`、`task_tools.py`、`memory_tools.py`，但现在 23 个工具仍能用一张显式表理解，不必为了形式立刻大改。

### 43.2 简写 schema 能力有限

`str`、`int=3`、`list[str]` 很容易读，但难表达复杂的条件关系。现在必须依赖 `validate_tool()` 做第二次校验。

### 43.3 shell 只读判断是启发式规则

它使用保守的命令白名单和危险词匹配，不可能理解所有 shell 语义。因此“无法确认就要求审批”是必要策略。

### 43.4 当前每轮只处理一个原生工具调用

控制简单稳定，但不支持真正的并行 tool calls。项目通过 `read_files` 降低常见批量读取的轮数。

### 43.5 `tool_steps` 名字容易被误解

它实际统计非缓存工具尝试，不只统计成功执行。界面若要更直观，未来可以同时展示 `tool_attempts`、`tool_successes`、`cache_hits`。

### 43.6 Trace 不是最佳的人类复盘界面

Trace 适合机器诊断，Session history 更适合直观看调用顺序。未来可以基于 Session 生成一份简洁时间线，而不是要求用户直接阅读 JSONL Trace。

---

## 44. 怎样测试工具调用系统

### 44.1 自动测试

重点测试文件包括：

```text
tests/test_hh_agent_pro_interaction_repair.py
tests/test_hh_agent_pro_extensions.py
tests/test_hh_agent_pro_codex_cli.py
```

运行：

```powershell
pytest -q
```

只跑工具交互相关测试：

```powershell
pytest -q tests/test_hh_agent_pro_interaction_repair.py tests/test_hh_agent_pro_extensions.py
```

现有测试覆盖的关键行为包括：

- DeepSeek 流式工具参数拼接；
- DeepSeek V4 `reasoning_content` 原样回传；
- assistant tool-call 与 tool result ID 配对；
- 文件未变化时缓存命中；
- 文件变化后缓存失效；
- 缓存不消耗工具额度；
- 只读 shell 分类；
- 审批拒绝立即安全停止；
- 相同失败第二次停止；
- 工具额度用完后 final-only 恢复；
- 最后一个工具执行后仍能给最终答案。

### 44.2 手动测试

启动：

```powershell
python -m hh_agent_pro
```

然后依次问：

```text
列出 docs 目录下的文件
```

观察是否调用 `list_files`。

```text
读取 README.md 前 50 行并总结
```

观察是否出现 `read_file`，并在下一轮回答。

```text
再读取完全相同的 README.md 前 50 行
```

在同一次 run 的自动模型循环中，重复读取应命中缓存；跨用户提问会重置本轮 ToolExecutor 缓存，不能把两次独立提问混为一谈。

```text
把 ../outside.txt 写入 hello
```

应被工作区边界拒绝。

```text
在项目里创建 temp_demo.txt，内容是 hello
```

在 `ask` 审批策略下应提示用户确认。

---

## 45. 看到终端输出时应该怎样解释

例如：

```text
Thinking - decision 2/36 | tools 1/12
* read_file {"path":"README.md"}
  [ok] ...
```

含义是：

- `decision 2/36`：正在进行第 2 次模型决策；
- `tools 1/12`：已经发生 1 次非缓存工具尝试；
- `read_file`：模型申请的工具；
- `[ok]`：ToolExecutor 给出的结构化成功状态。

它不表示：

- 打印了模型完整思维链；
- 模型已经直接接触本地函数；
- 每个 `decision` 都执行了工具；
- `tools 1` 一定代表一次成功写入。

---

## 46. 面试时可以怎样讲

你可以这样回答：

> HH Agent Pro 采用模型决策与本地执行分离的工具架构。工具通过显式注册表定义名称、参数 schema、描述、风险等级和 runner。对于 DeepSeek，我把这些定义转换为 OpenAI-compatible function schemas，通过原生 tools 字段发送；对于文本后端，则使用受控的 `<tool>` 协议。Provider 的输出最终都会归一成 `name + args`。
>
> AgentLoop 只负责控制循环和角色正确的历史记录，真正执行统一进入 ToolExecutor。ToolExecutor 按顺序做本轮白名单、工具存在性、参数与工作区路径校验、新鲜度缓存、重复调用检测、风险审批，再执行底层函数。执行结果统一封装为 `content + metadata`，metadata 包括状态、错误码、是否可重试、受影响路径和建议动作，随后作为 tool role 消息回填给下一轮模型。
>
> 我还把模型决策次数和有效工具尝试次数分开，默认允许 12 次工具尝试和最多 36 次模型决策。工具额度用完后会进入 final-only，让模型使用已有证据完成总结；相同失败第二次出现会停止，避免死循环。DeepSeek V4 工具轮次所需的 `reasoning_content` 只作为私有协议状态原样回传，不暴露为用户可见思考。

如果面试官继续追问安全性，可以补充：

> Prompt 只负责引导，真正安全边界在确定性代码中，包括根目录路径解析、保守的 shell 风险识别、审批策略、密钥脱敏以及风险操作前后的工作区快照差异。

---

## 47. 最终背诵卡片

### 47.1 四个核心对象

```text
tools.py          = 工具菜单和真实工具函数
AgentLoop         = 决策循环和消息传递
ToolExecutor      = 校验、审批、执行、防循环总闸门
ToolContext       = 底层工具的受限依赖包
```

### 47.2 八步主流程

```text
1. Runtime 显式注册工具
2. schema 通过原生 API 或 Prompt 给模型
3. 模型返回一个 name + args 申请
4. Provider/Parser 转成统一内部格式
5. AgentLoop 记录 assistant tool_call
6. ToolExecutor 校验、审批并执行
7. 结果记录为 role=tool，与 call_id 配对
8. 下一轮模型读取结果，继续或最终回答
```

### 47.3 三条最重要原则

```text
模型有建议权，没有执行权。
Prompt 负责引导，程序代码负责强制安全。
工具结果必须回到下一轮，模型才能基于真实观察继续。
```

### 47.4 三个常见误区

```text
attempts 不是工具次数。
tool_steps 不是成功次数，而是非缓存工具尝试次数。
Trace、Evidence、Memory 是旁路观察和上下文系统，不是工具执行器。
```

只要你能清楚解释这四个对象、八步流程和三个原则，就已经能够完整讲明 HH Agent Pro 的工具调用设计。
