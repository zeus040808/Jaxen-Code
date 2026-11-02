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
