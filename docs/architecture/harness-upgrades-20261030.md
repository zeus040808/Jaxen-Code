# Harness 升级：预算、工具与控制循环（2026-10-30）

对照 Claude Code 的设计，把 harness 从「省上下文、多替模型做决定」改成
「给足上下文和预算、工具顺手、少干预」。上下文部分（P0-1/2）见
[context-engineering.md](context-engineering.md)（早期两步过渡改动见 [context-management-v1.md](../history/context-management-v1.md) 第 14、15 节），这里记录其余改动。

## 改动一览

| 编号 | 改动 | 位置 | 回归测试 |
|---|---|---|---|
| P0-4 | 预算放大：步数 12→50、输出 2048→8192 token、单轮 3→20 分钟、shell 超时默认 20→120 秒、上限 120→600 秒 | `agent.py`、`cli.py`、`tools.py` | `test_codex_cli.py`、tool-contract `timeout_out_of_range` |
| P1-a | 编辑工具：覆盖前必须先读、读后被改则拒绝、编辑返回 diff、`read_file` 默认 2000 行并按整行截断、新增 `glob` | `tool_executor.py`、`tools.py` | `test_edit_tools.py` |
| P1-c | 交付门禁只提醒一次，之后放行并照实披露；不可重试的失败不再算阻塞 | `agent_loop.py`、`agent.py` | `test_delivery_gate_deadlock.py` |
| P1-b | 一轮多个工具调用：2~4 个只读并发，其余按顺序执行，上限 8 个 | `agent_loop.py`、`agent.py` | `test_sequential_batch.py` |
| P1-e | 会话内轻量 todo（`todo_write`，整体替换） | `task_state.py`、`tools.py` | `test_todo_tool.py` |
| P1-d | `--toolset core|full`；系统提示只讲已暴露工具的规则 | `agent.py`、`tools.py`、`prompt_prefix.py`、`cli.py` | `test_toolset.py` |

## 关键设计取舍

### 先读再改（P1-a）

| 情况 | 规则 | 理由 |
|---|---|---|
| `write_file` 覆盖已有文件 | 必须先读 | 整文件盲写风险最大 |
| `patch_file` 改没读过的文件 | 允许 | `old_text` 必须唯一匹配，已证明模型知道内容 |
| 读过之后文件又被改（shell、用户或别的进程） | 拒绝，`file_changed_since_read` | 模型依据的是过期内容 |
| 新建文件 | 不受限 | — |

最初的版本对 `patch_file` 也强制先读，结果确定性回归里 8 个脚本化场景
（直接 patch、且只允许 `patch_file`）全部被拒，runtime 通过率掉到 43%。
`patch_file` 的唯一匹配本身就是内容校验，所以放宽。

`read_file` 不走头尾截断：代码中间几百行悄悄消失比截断更危险。超长时在整行处
截断，并提示 `call read_file with start=N to continue`。

### 交付门禁（P1-c）

实测强制放行的 5 次里 4 次是「改了代码但没跑通测试」。门禁反复驳回会逼模型自己写
测试文件来过门禁——flash 跑 11 个任务里 9 个因越界改测试被判 unsafe。现在：

- 只提醒一次，措辞是建议而不是拒绝；
- 仍未解决就放行，答案末尾附 `Unresolved at delivery`，stop_reason 仍是
  `incomplete_delivery`，结果照实，不粉饰；
- 不可重试的失败（如 `path_escape`）不算阻塞。这条修掉了一个会话前就存在的
  确定性回归失败（`security_recovery` 场景）。

### 工具集（P1-d）

- CLI（交互与 JSONL/评测）默认 `core`：11 个编码工具。
- Python API 默认 `full`，保持对现有调用方和单元测试的兼容。
- 跨会话记忆自动沉淀、cron 等后台服务不受工具集影响。
- 文本模式 prompt 从 9076 字符降到 5730 字符（-37%）。
- 新增规则「只做被要求的事；除非用户要求，不新增、修改或删除测试文件」。这是通用的
  范围纪律（Claude Code 同样要求 do what has been asked, nothing more），不是针对评测
  的特判。

## 验证

- `pytest`：205 passed, 4 skipped；唯一失败
  默认模型断言（代码默认值已改为 deepseek-flash，测试随后修正）。
- `scripts/eval/run_deterministic_gate.py --platform linux`：runtime 14/14、
  tool-contract 100%、security 100%。
- 真实模型评测：见 SWE-bench 相关文档。

## 第二轮：provider 精简与上下文工程重构

| 改动 | 内容 |
|---|---|
| provider | 删除 Ollama、Codex CLI、Responses API、Anthropic 四种 client；统一为 `complete(ModelRequest)`。保留 `OpenAIChatClient`（任意 OpenAI Chat Completions 兼容端点，DeepSeek 为预设）与测试/评测用的脚本化模型 |
| 上下文 | 删除 8 分区字符配额、24000 总预算与文本/原生双路径，改为"稳定 system + 只追加会话记录 + 末尾提醒"，token 预算由模型窗口推导，60% 轻压缩、80% 摘要压缩、兜底裁剪，详见 [context-engineering.md](context-engineering.md) |
| system | AGENTS.md / CLAUDE.md 完整注入（旧实现每份截到 1200 字符，且整体被预算截光）；环境快照在会话开始时冻结，改文件不再让缓存失效 |
| 评测 | 上下文消融实验（测的是旧分区设计）删除；`context_reduction_checkpoint` 场景改为 token 预算并关闭 LLM 摘要以保持确定性 |

验证：`pytest` 209 passed；确定性回归 runtime / tool-contract / security 均 100%；pyflakes、`tsc --noEmit` 通过。

## 第三轮：验证门禁、子 agent、请求重试

| 改动 | 之前 | 之后 | 回归测试 |
|---|---|---|---|
| 验证门禁 | "改了代码没跑通测试"阻止交付；识别靠正则，`cd x && pytest`、复现脚本都算没验证（SWE-bench 冒烟误报 `incomplete_delivery`） | 只在末尾提醒里显示 `Code verification: pending test`，不阻止交付；开头的 `cd … &&` 可识别，`; true`、`|| true` 这类能掩盖失败的写法仍不算 | `test_core_capacity.py`、`test_delegate_and_verification.py` |
| 子 agent | 默认 3 步，只够读两三个文件 | 默认 20 步、上限 30，沿用主 agent 的总时长；工具说明与提示规则明确"大范围只读调查交给它，主上下文保持精简"；仍只读，编辑与集成由主 agent 负责 | `test_delegate_and_verification.py` |
| 请求重试 | 只对 5xx 重试 3 次、间隔 0.5 秒；429、超时、流中断直接让任务失败 | 429/408/409/425/5xx/529、超时、连接错误、流读到一半中断都按指数退避加抖动重试，最多 6 次、单次最长 60 秒，优先遵守 `Retry-After`；400/401/402/403/404 立即失败；重试过程在终端显示 | `test_model_retry.py` |

验证：`pytest` 231 passed；确定性回归 runtime / tool-contract / security 均 100%；pyflakes 干净。

## 第四轮：代码定位（为大仓库、中等题做准备）

问题：SWE-bench 镜像里没有 `rg`，`search` 实际退化成"不区分大小写的子串匹配"——不支持正则、不能按文件类型过滤、
没有上下文行、最多 200 条且不提示如何缩小；也没有办法不读整个文件就知道函数/类在哪里。简单题靠报错信息一步就能找到文件
（easy20 中位第 2 次工具调用就读到正确文件），但中等题多文件比例是简单题的 5 倍，定位会成为瓶颈。

| 改动 | 之前 | 之后 | 回归测试 |
|---|---|---|---|
| `search` | 有 `rg` 用 `rg`，否则子串匹配；遍历整棵目录（含 `.venv`、`node_modules`，本仓库一次 18.6 秒） | 统一用 Python 正则（任何环境行为一致）；smart-case；`glob` 过滤；`output_mode` = content / files / count；`context` 上下文行（`:` 命中、`-` 上下文、`--` 分块，同 grep）；`max_results` 默认 100、上限 500，截断时提示"用 path/glob 缩小或改用 files 模式"；文件列表用 `git ls-files`（遵守 `.gitignore`，0.03 秒）；跳过二进制与 2MB 以上文件 | `test_code_search.py` |
| `glob` | `Path.glob` 遍历全部文件 | 同样走 `git ls-files`，保持 `Path.glob` 语义（不带 `/` 只匹配当前层，`**/` 匹配任意层） | `test_code_search.py` |
| `repo_map`（新） | 无 | 用 `ast` 列出 Python 文件的类/函数/方法及签名和行号；带 `pattern` 时就是"跳转到定义"（`path:line: def name(args)`）；解析结果按 mtime 缓存；输出有预算，超出提示缩小范围；语法错误文件跳过，非 Python 给说明。属于只读工具，可并行，子 agent 也能用 | `test_code_search.py`、`test_toolset.py` |
| 提示规则 | 只有"优先用专用工具" | 新增"先定位再修改"：从报错、异常类型、函数名、路径、失败测试名里提线索 → `search(output_mode='files')` / `repo_map(pattern=…)` 得到候选 → 只读相关函数 → 能复现先复现 → 改签名或行为前先搜调用点 | — |
| 定位指标（新） | 只看最终 resolved | `scripts/swebench/localization_metrics.py`：以官方补丁涉及的文件为标准，从 trace 计算第几步读到正确文件、之前绕了几个文件、读到/改到的召回率、改动精确率 | — |

django 镜像（6649 个文件）实测：`search` files 模式 1.2 秒、count 0.3 秒；`repo_map` 全仓找定义首次 1.8 秒、缓存后 0.13 秒。

取舍：
- 不引入 tree-sitter 等依赖，`repo_map` 只支持 Python（SWE-bench Verified 全是 Python 仓库）；其他语言靠 `search`。
- 不做向量检索/嵌入：对"报错信息 → 文件"这种定位，正则 + 定义索引已经够用，且结果可解释、零成本。
- 改进效果还没有在中等题上验证；需要用定位指标对比改动前后的 trace 才能下结论。

修复（难题评测中发现）：模型调用不存在的工具（如把 `run_shell` 写成 `shell`）被记为"可重试失败"。只有同名工具后来成功一次才会标为已解决，
而不存在的工具永远不会成功，于是交付时一直挂着 `unresolved failure shell:unknown_tool`，被误报为 `incomplete_delivery`（django-11138，实际已通过判分）。
改为 `retryable=False`：模型仍会收到"从已列出的工具里选"的提示，但这次失败不再阻止交付。回归测试：`test_core_capacity.py::test_unknown_tool_name_does_not_block_delivery`
（修复前失败、修复后通过）；确定性场景 `tool_unknown_name_recovery` 仍通过。

验证：`pytest` 247 passed、4 skipped；确定性回归 runtime / tool-contract / security 均 100%；pyflakes 干净。

修复二（隔离重跑中发现，同类第二例）：只读工具的参数错误也会一直挂到交付。django-11265 里 agent 读了一个不存在的路径
（`django/tests/...`，正确的是 `tests/...`），之后改对路径、改完代码、跑过 31 个测试，交付时仍报 `incomplete_delivery`。
原因同上：一次失败要等"同一个动作后来成功"才算解决，而错误的路径永远不会成功。

没有沿用上次"改成不可重试"的做法：`retryable` 会展示给模型（"改好参数可以重试"对参数错误是正确提示），也被用来判断重复失败。
改为调整"什么算已解决"：**只读工具的 `invalid_arguments`，之后任意一次工具调用成功就算已解决**（`task_state.superseded_by_progress`，
任务状态和工具执行器两处共用）。两条原有保护不变，都有测试守着：

- 失败之后一次成功都没有就说"完成"：仍会被拦下（`test_extensions.py::test_tool_failure_feedback_is_structured_and_defers_false_final`）。
  第一版改法让只读参数错误完全不阻塞，把这条保护也拆了，被这个测试挡住后改成现在的规则。
- 修改类工具失败：`patch_file` 找不到原文同样是 `invalid_arguments`，但那是想改的地方没改成，仍必须由同一个改动成功来解决
  （`test_core_capacity.py` 的两个用例）。

刻意没改的一类：`run_shell` 跑测试失败（如 `python` 不存在），之后用 `python3 … || …` 成功，仍报未解决。
带 `||` 的命令可能掩盖失败，而那次 pytest 实际上也确实没跑起来（环境里没装），这个提示是真实的。

回归测试：`test_core_capacity.py::test_rejected_arguments_do_not_block_delivery`（修复前失败、修复后通过）。
验证：`pytest` 265 passed、4 skipped；确定性回归 runtime / tool-contract / security 均 100%；pyflakes 干净。

修复三（Opus 实验中发现）：防死循环规则把正常工作误判成死循环。django-12325 里 Opus 改了又撤回（文件恢复原样）后回头看代码：
读缓存按"文件内容哈希"命中，只返回一句"内容没变，用已有证据"的摘要而不是代码；它再读一次，同一段内容累计第 3 次命中，
harness 进入 final-only、禁用全部工具。此时 Opus 已得出和官方一致的修法，却只能交空补丁。

改法（`tool_executor.py`，两行）：
- 每真正执行一次工具，重复读取计数清零：只有原地**连续**反复读同一段、中间毫无进展才算死循环（原有的连续重复测试仍通过）。
- 工作区有改动时清空读缓存：改过（哪怕又撤回）之后再读，返回真实内容。

flash 探索少、很少改了又撤回，所以之前没暴露；换成探索更多的 Opus 才踩到。
回归测试：`test_core_capacity.py::test_reread_after_edit_and_revert_returns_real_content`、`test_repeat_count_resets_after_real_progress`（修复前都失败）。
验证：`pytest` 277 passed、4 skipped；确定性回归均 100%；pyflakes 干净。
真实任务复测：Opus 重跑 django-12325，缓存命中 0 次、不再误判；这次是 50 步预算用完才进入 final-only，补丁已应用（核心修改正确，仍因保留旧报错未通过判分）。

修复四（新中等题评测中发现）：模型空响应导致整题失败。django-12406 第 18 轮，DeepSeek thinking 模式只返回了推理、没有正文也没有
工具调用（恰好发生在一次轻量上下文清理之后，是否有关只有这一例，无法确定），provider 抛出 "could not extract text"，
AgentLoop 记为 `model_error`，前面 18 轮、65 万 token 的进度全部作废。

改法（`providers/openai_chat.py`、`providers/anthropic_messages.py`）：
- 没有正文、没有工具调用、也不是被截断：**原样重发同一个请求**，最多 2 次（`EMPTY_RESPONSE_RETRIES`）。请求没有副作用，
  输出有随机性；重发过程会在终端/界面显示。
- 重发后仍为空才报错，错误信息里带上 `finish_reason`（以前失败时不记录，无法判断原因）。
- 空但原因是输出额度用光（`length` / `max_tokens`）：不报错，返回空文本，交给 AgentLoop 已有的截断恢复。
- 重发那几次的 token 也累加进用量（`merge_usage`），预算统计不漏算；`empty_response_retries` 记录重发次数。

回归测试：`test_model_retry.py` 3 个、`test_anthropic_provider.py` 2 个（修复前都失败）。
验证：`pytest` 282 passed、4 skipped；确定性回归均 100%；pyflakes 干净。

## 第五轮：中等 50 道评测后的修复与复盘证据（2026-11-03）

依据：[中等题 50 道统一版本测量](../results/2026-11-03-swebench-medium50.md)。

修复五：只读工具运行时出错导致交付误拦。sphinx-8638 猜错文件名，`search` 一个不存在的 `sphinx/directive.py`，
这是运行时出错（`tool_failed`，Errno 2），不是参数校验失败（`invalid_arguments`），不适用修复二的规则，一直挂到交付，
被提醒一次后强制交付成 `incomplete_delivery`。

改法（`task_state.superseded_by_progress`，一行）：只读工具的 `tool_failed` 和 `invalid_arguments` 同样处理——之后有任意一次
工具调用成功就算已解决；失败之后一次成功都没有就说"完成"，仍然拦下。会改东西的 shell 命令（测试命令）和修改类工具不受影响：
测试失败不能被一次成功的读取"绕过去"。
回归测试：`test_core_capacity.py::test_failed_read_only_search_does_not_block_delivery`（修复前失败）、
`test_failed_shell_check_still_blocks_delivery`（守住测试失败仍拦截）。

复盘证据：trace 里工具结果只留 500 字符、请求只留 300 字符，模型的思考内容和每轮完整上下文都没有记录，复盘时分不清
"模型没看到关键报错"还是"看到了没理会"；trace 里也没有模型名和代码版本。改法：

| 改动 | 做法 | 位置 |
|---|---|---|
| 完整对话记录 | 每个 run 目录新增 `transcript.jsonl`：每轮 `request`（`kept_messages` 条沿用上一轮 + `new_messages`；system 变了才重写）、`response`（原始返回、思考内容、`finish_reason`）、`model_error`、`final`。按 kept + new 逐轮能还原模型收到的全部消息，包括工具结果全文、注入的运行时提示和摘要；只写增量，文件大小随轮数线性增长 | `agent_loop._transcript_request/_transcript_response`、`run_store.append_transcript` |
| 思考内容 | provider 把 `reasoning_content` 存到 `last_reasoning_content`，只进 transcript，不进 UI 和 trace | `providers/openai_chat.py` |
| 版本和配置 | `run_started` 新增 `provider`、`model`、`agent_version`（hh_agent_pro 全部 .py 的内容哈希，评测容器里没有 .git、未提交改动也能区分）、`config`（步数、输出上限、时长、审查档位、工具集） | `agent_loop.agent_source_hash` |
| 父子 run 配对 | 子 agent（调查 / 审查）的 `run_started` 记 `parent_run_id` | `agent.readonly_child` |

transcript 同样经过 `redact_artifact` 脱敏（环境变量里的密钥替换掉）。
回归测试：`tests/test_transcript.py` 3 个（逐轮还原请求、工具结果全文、思考内容、模型报错、版本配置和父 run）。
验证：`pytest` 312 passed、4 skipped；确定性回归均 100%；pyflakes 干净。

修复六：改完代码后重跑同一条失败命令被判为"重复失败"。django-12754：复现脚本失败 → 改代码 → 再跑同一个脚本，
失败指纹（工具名 + 参数 + 错误码）和上一次相同，第 2 次就触发 `repeated_failed_call`，整个运行被停掉（补丁其实已经对了）。
改法（`tool_executor.py`）：一次**成功**的工具调用改动了工作区，就把失败计数清零——中间改过文件，再跑同一条命令是"改完验证"。
只认成功的改动：失败命令自己顺带写了文件（`partial_success`）不算进展，否则真正的循环永远停不下来。
回归测试：`test_core_capacity.py::test_rerunning_failed_command_after_edit_is_not_a_loop`（修复前失败）、
`test_rerunning_failed_command_without_progress_still_stops`（原样重跑仍然停下）。

修复七：正文为空的截断。sympy-23413 连续两轮 8192 个输出 token 全被思考内容用完，正文 0 字符；原有的截断恢复是
"禁用工具、接着草稿往下写"，草稿是空的，第二轮照样截断，整题以 `output_truncated` 失败。
改法（`agent_loop.py`、`context_manager.py`）：被截断而正文为空时，不进续写模式，而是把本次运行的输出上限提高到 4 倍
（不超过模型上限，`max_new_tokens_override`），工具照常可用，重发这一轮；只提高一次，之后仍为空就交给原有逻辑。
按实际用量计费，上限高不额外花钱。trace 记 `output_budget_raised`。
回归测试：`test_interaction_repair.py::test_empty_truncation_raises_output_budget_and_keeps_tools`（修复前失败）、
`test_empty_truncation_raises_budget_only_once`。

修复八：经过代理时 HTTP 重试全部失败（这次 2 道"基础设施失败"的真正原因）。sympy-13551、scikit-learn-25747 每次都是
DeepSeek 先返回 503，之后的重试全部被出口代理以 403 拒绝，目标变成 `api.deepseek.com:80`。根因在 provider：
重试复用同一个 `urllib.request.Request`，而 urllib 的 ProxyHandler 会在 Request 上原地设置隧道；第二次使用时
它退化成 `type=http`、隧道目标不带端口，于是发出 `CONNECT api.deepseek.com:80`。也就是说**只要经过代理，第一次失败之后的
重试必然失败**，公司网络里日常使用同样会碰到。补跑时有了 transcript，才从"DeepSeek 不稳定"的猜测定位到这里。
改法（`openai_chat.py`、`anthropic_messages.py`）：每次重试都新建 Request。
第一版修复自己又引入了一个 bug：请求体变量叫 `body`，和 HTTPError 分支里存错误响应的 `body` 同名，503 之后请求体被覆盖成 str，
重试时报 `TypeError: POST data should be bytes`。单元测试的假 urlopen 不检查请求体，所以没发现，补跑时才暴露。
已改名为 `request_body`，两个 provider 的重试测试都加了"请求体必须是 bytes"的断言（重新引入同名变量时两个测试都会失败）。
回归测试：`test_model_retry.py::test_retry_behind_proxy_keeps_the_https_tunnel`（模拟 ProxyHandler 的 set_proxy；修复前失败）、
`test_anthropic_provider.py::test_rate_limit_is_retried`。

还没修、留待讨论：测试命令因环境问题失败（如容器里没有 pytest）也会拦截交付（django-16662、astropy-8707）。
它们最终都通过了，拦截没有造成错误结果，但信号不准；是否只拦"最后一次测试失败"需要更多数据再定。

trace 完整性的其余补充：

| 缺口 | 做法 | 位置 |
|---|---|---|
| `egress_denied` 一直是 0 | shell 子进程继承 `HTTP(S)_PROXY`、`NO_PROXY`（大小写两种）：评测里 agent 的 pip/curl 会经过出口代理、被拒绝并计数；公司网络里日常用 pip 也靠它。密钥仍不继承 | `agent.DEFAULT_SHELL_ENV_ALLOWLIST`；测试 `test_eval_isolation.py::test_shell_commands_inherit_proxy_but_not_secrets` |
| 同一道题多次运行的 trace 混在一起 | 评测 trace 改存到 `traces/<运行名>/<实例>/`；读取时新布局优先、没有就退回旧的 `traces/<实例>/` | `run_hh_predictions.py`、`localization_metrics.instance_trace_dir` |
| 补丁、失败测试、trace 分散三处 | `scripts/swebench/trace_report.py`：整批每题一行的模块触发统计（写出 `<运行名>.trace-report.json`）；`--instance` 输出单题时间线（版本配置、逐步工具调用、关键事件、失败的隐藏测试、transcript 位置） | 测试 `tests/test_trace_report.py` |

验证（第五轮全部改动）：`pytest` 321 passed、4 skipped；确定性回归均 100%；pyflakes 干净。

## 第六轮：故障注入发现的问题（2026-11-05）

模块评测第一项（[故障注入评测](../evaluation/fault-injection.md)）发现：原生 tool_calls 的参数是半截 JSON 时，
`OpenAIChatClient` 直接抛 `RuntimeError`，整轮以 `model_error` 结束；同样坏掉的文本格式工具调用却会让模型重来。

改法（`providers/openai_chat.py`）：解析失败时不抛错，交回一个同样解析不了的文本工具调用，`agent.parse` 返回
`retry`，AgentLoop 追加纠正提示、让模型重来；同时清空 `last_native_assistant_message`，这次回复不进历史，
后续请求里不会出现没有工具结果的 tool_call id（真实 API 会因此返回 400）。completion metadata 记
`malformed_native_tool_arguments`，复盘时可查。

验证：故障注入场景 `bad_native_tool_args` 由 model_error 变为救回；核对了之后每次请求都没有孤立的 tool_call id；
回归测试 `tests/test_model_retry.py::test_malformed_native_tool_arguments_become_a_retry_not_a_model_error`。

另外两项（同日，压缩评测与 transcript 复盘发现）：

- **search / glob / repo_map 会搜到 agent 自己的 `.hh/`**：git 仓库里用 `git ls-files -co` 列文件，未跟踪文件也会列出，
  目标仓库的 `.gitignore` 不认识 `.hh`。SWE-bench transcript 里出现过 search 结果是 `.hh/runs/.../run_state.json`。
  改为对 git 列出的文件也按忽略名单过滤（`tools._project_files`）；回归测试 `tests/test_code_search.py::test_search_glob_and_repo_map_skip_the_agent_state_dir_in_git_repos`。
- **兜底裁剪丢掉长请求的后半段**：见 [压缩评测](../evaluation/context-compaction.md)。裁剪后请求全文不在了，留出位置重新裁一次，
  把全文放回末尾提醒（`context_manager.prepare`）。
- **摘要请求被模型当成对话接着写**：真实模型评测 28 次摘要只有 50% 合格，29% 输出工具调用和假的文件内容却被当成摘要接受。
  改为原文包标签 + 结尾指令、输出上限 16384、输出校验、同一运行失败后不重试；复测 28/28 合格。见 [压缩评测](../evaluation/context-compaction.md)。

