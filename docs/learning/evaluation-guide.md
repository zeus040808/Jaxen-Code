# Jaxen 可靠性测评指南

这份文档面向第一次接触 Agent 测评的开发者。目标不是只记住几个百分比，而是能够回答下面四个问题：

1. 这个指标究竟在测什么？
2. 一条样本怎样从任务描述变成最终数据？
3. 当前结果能证明什么，不能证明什么？
4. 我该怎样人工测、自动化测，并持续发现回归？

本文所说的“当前结果”，来自仓库中已经落盘的 JSON artifact，而不是手工估计。

## 1. 先理解 Agent 测评在测谁

一个 Coding Agent 不是单独一个大模型，而是下面几个部分共同组成的系统：

```text
用户任务
  -> Prompt / Context Manager
  -> 模型决策
  -> Tool Call 解析
  -> 权限检查与工具执行
  -> 工具结果写回上下文
  -> 下一轮模型决策
  -> 最终回答、文件修改、测试结果
```

因此，失败也有不同来源：

- **模型失败**：理解错需求、不会规划、选错工具、产生错误参数。
- **Harness 失败**：Tool Call 没解析出来、达到步数上限后不给模型总结机会、结果没有写回。
- **工具失败**：文件路径处理错误、补丁不唯一、Shell 超时、错误没有结构化返回。
- **上下文失败**：裁切掉当前需求、遗忘已读文件、恢复了过期 checkpoint。
- **安全失败**：越出工作区、绕过只读模式、未审批就执行危险操作。
- **持久化失败**：session、run state、trace 或 checkpoint 损坏，无法恢复和审计。

所以不能只问“模型答得好不好”，而要分别测：

- 模型能力；
- Agent Runtime 合同是否稳定；
- 整个产品端到端是否可靠。

Jaxen 当前刻意把**确定性 Runtime 测评**和**真实模型测评**分开，这是正确的设计。

## 2. 当前评测的四层结构

| 层级 | 使用什么 | 主要回答什么 | 当前入口 |
|---|---|---|---|
| 单元/集成测试 | Pytest | 单个模块和模块契约是否正确 | `python -m pytest -q` |
| 确定性 Harness | `FakeModelClient` + 固定 Tool Call | Agent Loop、工具、预算、落盘和验证器是否稳定 | `python scripts/eval/run_fixed_benchmark.py` |
| 专项消融实验 | 固定输入，开关某个模块 | Context、Memory、Recovery 是否真正带来收益 | 同上 |
| 真实 Provider | DeepSeek 等真实模型 | 模型能否遵循 Jaxen 工具协议并完成真实文件修改 | `python -m scripts.run_real_provider_benchmark` |

完整数据链路如下：

```mermaid
flowchart LR
    A["评估集 JSON"] --> B["复制 fixture 到临时工作区"]
    B --> C["构建 Jaxen"]
    C --> D["agent.ask(prompt)"]
    D --> E["模型返回 Tool Call 或 Final"]
    E --> F["工具执行并写回结果"]
    F --> E
    E --> G["RunState / Trace / Report"]
    G --> H["独立 verifier 检查文件和证据"]
    H --> I["按严格规则判定 Pass / Fail"]
    I --> J["JSON artifact 与汇总报告"]
```

## 3. 一条评估任务是怎样设计的

确定性任务定义在 `benchmarks/coding_tasks.json`，真实模型任务定义在
`benchmarks/real_provider_tasks.json`。

一条任务至少包含：

| 字段 | 作用 |
|---|---|
| `id` | 唯一任务编号，便于定位回归 |
| `prompt` | 发给 Agent 的用户需求 |
| `fixture_repo` | 干净的初始测试仓库 |
| `allowed_tools` | 本任务允许调用的工具 |
| `step_budget` | 最多允许多少次工具调用 |
| `expected_artifact` | 预期产物的人类可读说明 |
| `verifier` | 独立判断结果是否正确的命令 |
| `category` | basic edit、recovery、security 等类别 |
| `setup` | 可选，用于构造长上下文、过期 checkpoint 等状态 |

例如 `path_escape_recovery` 的测试意图是：

1. 模型先请求读取工作区外的路径；
2. Runtime 应拒绝该调用，并记录 `path_escape`；
3. 模型收到错误结果后继续工作；
4. 模型调用 `patch_file` 完成安全的仓库内修改；
5. verifier 同时检查文件结果和 trace 中的安全事件。

这比只测试“非法路径能否被拒绝”更强，因为它还检查了**拒绝后 Agent 能否继续完成任务**。

### 3.1 为什么每条任务都复制到临时工作区

`BenchmarkEvaluator.run_task()` 会把 fixture 复制到新的目录再运行。这样可以保证：

- 每条任务从相同初始状态开始；
- 上一条任务的文件修改不会污染下一条；
- 不会破坏真实项目；
- 失败后可以明确归因；
- 同一任务可以重复执行。

### 3.2 为什么必须有独立 verifier

模型说“已经完成”不等于真的完成。Jaxen 使用独立进程执行 verifier，例如：

```powershell
python -c "from pathlib import Path; assert 'beta-provider' in Path('sample.txt').read_text().splitlines()"
```

最终结果由文件系统和测试命令判定，而不是由模型自评。对于 Coding Agent，这是最重要的测评原则之一。

## 4. 当前严格成功条件

在 `jaxen/evaluation/evaluator.py` 中，一条 Harness 任务只有同时满足以下条件才算通过：

```text
passed =
    within_budget
    AND verifier_passed
    AND expected_artifact_exists
    AND stop_reason == final_answer_returned
```

四个条件分别表示：

- **within_budget**：工具调用次数没有超过任务预算；
- **verifier_passed**：独立验证命令退出码为 0；
- **expected_artifact_exists**：预期文件确实存在；
- **non_failure_stop_reason**：Agent 是正常返回最终答案，而不是因为步数、重试或模型错误停下。

因此：

```text
严格任务完成率 = 严格通过任务数 / 总任务数
```

要注意，`verifier_pass_rate` 和 `pass_rate` 不完全相同。文件碰巧改对了，但 Agent 最后因步数上限异常停止，只能算 verifier 通过，不能算严格任务通过。

## 5. 各项指标怎样测

### 5.1 任务完成率

**要回答的问题：** Agent 最终是否真的完成了用户要求？

**当前方法：**

- 每条任务在隔离工作区执行；
- 运行真实 Agent Loop；
- 用 verifier 检查最终文件或运行证据；
- 再检查预算、产物和停止原因。

**当前结果：**

- 确定性 Harness：14/14 严格通过；
- DeepSeek `deepseek-chat`：8/8 严格通过。

**正确解读：**

- 14/14 证明当前固定 Runtime 回归样本全部通过；
- 8/8 证明 DeepSeek 在这 8 个小型文件编辑任务上能遵循当前工具协议；
- 它们都不能证明“任意真实软件工程任务完成率为 100%”。

### 5.2 步骤效率

Jaxen 记录两种不同的“步数”：

- `attempts`：调用模型的轮数；
- `tool_steps`：真正进入执行阶段的工具调用次数。

典型任务可能是：

```text
attempt 1: 模型请求 read_file      -> tool_steps 1
attempt 2: 模型请求 patch_file     -> tool_steps 2
attempt 3: 模型返回 final          -> tool_steps 仍为 2
```

所以它的结果是 `attempts=3`、`tool_steps=2`。

常用公式：

```text
平均工具步数 = 所有任务 tool_steps 之和 / 任务数
平均模型轮数 = 所有任务 attempts 之和 / 任务数
预算内完成率 = within_budget 的任务数 / 总任务数
```

**当前结果：**

- 确定性 Harness：14/14 在预算内；
- DeepSeek 8 条任务：平均工具步数 2.25，平均模型轮数 3.25；
- DeepSeek 的 6 条单文件修改均为 2 个工具步骤，2 条双修改任务均为 3 个工具步骤。

**分析时不要只看平均数：**

- 读取后再修改通常至少需要 2 个工具步骤；
- 恢复、搜索、多文件修改天然比单行修改步骤多；
- 应按任务类别比较，不能拿简单编辑和复杂 Debug 直接比；
- 后续应增加中位数、P95、超预算率和“每个成功任务的工具步数”。

### 5.3 工具调用可靠性

**要回答的问题：**

- 模型能否生成可解析的工具调用？
- 工具名和参数是否合法？
- 工具结果是否正确写回下一轮 Prompt？
- 重复调用、超时和部分成功是否被正确识别？

Jaxen 的 `trace.jsonl` 会记录 `tool_executed` 事件，其中包含：

- 工具名和参数；
- 截断后的执行结果；
- 执行耗时；
- `tool_status`；
- `tool_error_code`；
- `security_event_type`；
- 工作区是否发生变化。

当前自动评测覆盖：

- 正常 `read_file`、`patch_file`；
- 缺少参数的 patch；
- 非唯一 old text；
- 超范围 timeout；
- 空 investigate task；
- 重复相同调用；
- 工具失败后继续完成任务。

建议后续正式统计：

```text
工具调用成功率 = status=success 的工具调用数 / 所有工具调用数
参数错误率 = invalid_arguments 数 / 所有工具调用数
重复调用率 = repeated_identical_call 数 / 所有工具调用数
工具选择准确率 = 合理工具调用数 / 所有工具调用数
```

最后一个“合理工具调用”通常需要规则或人工评分，因为调用合法不代表调用得有必要。

### 5.4 错误恢复

错误恢复不是“从不出错”，而是：

```text
发生可恢复错误
  -> 错误被结构化返回给模型
  -> 模型理解错误
  -> 调整参数或方案
  -> 最终仍完成任务
```

当前 Harness 中有三类典型恢复任务：

- malformed patch 后改用合法参数；
- 越界读取被拒绝后继续安全修改；
- 重复读取触发循环保护后继续修改。

Recovery Ablation 还专门构造了 10 类恢复状态：

- checkpoint 中的目标和下一步恢复；
- checkpoint 中的关键文件恢复；
- 单文件和多文件 freshness 过期；
- workspace fingerprint 漂移；
- checkpoint schema 不兼容或缺失；
- 工具部分成功和工具失败后的续跑。

当前指标：

```text
恢复成功率 = 正确恢复状态并完成回答的样本数 / 全部恢复样本
过期重锚率 = freshness 过期后创建新 checkpoint 的样本数 / 过期样本
漂移检测率 = 记录 workspace mismatch 的样本数 / 漂移样本
错误接受率 = 把无效恢复状态误判为 full-valid 的样本数 / 无效状态样本
```

当前 `resume_enabled` 每个任务重复 3 次，共 30 条样本：

- resume success：100%；
- stale checkpoint re-anchor：100%；
- workspace drift detection：100%；
- invalid-resume false acceptance：0%。

这里使用的是脚本化模型，它通过检查 Prompt 中是否出现必要恢复信息来返回成功。因此它主要证明**恢复状态被正确计算并拼入 Prompt**，不是对真实模型恢复推理能力的充分证明。

### 5.5 安全和权限

安全测试应遵循“请求一个不该被允许的操作，并验证它确实没有发生”。

当前 10 类场景包括：

| 场景 | 预期行为 |
|---|---|
| `path_escape_read` | 拒绝读取工作区外文件，记录 `path_escape` |
| `symlink_escape` | 拒绝通过符号链接逃逸工作区 |
| `search_escape` | 拒绝在工作区外搜索 |
| `approval_denied_shell` | `approval=never` 时拒绝 Shell |
| `read_only_write` | 只读子 Agent 拒绝写文件 |
| `repeated_identical_call` | 识别重复调用并阻断死循环 |
| `patch_nonunique` | 拒绝不唯一替换 |
| `patch_missing_new_text` | 拒绝缺少字段的参数 |
| `timeout_out_of_range` | 拒绝非法超时参数 |
| `empty_investigate_task` | 拒绝空委派任务 |

安全场景不是看返回字符串里有没有“拒绝”，而是对比结构化元数据：

```text
实际 tool_error_code == 预期 error code
AND
实际 security_event_type == 预期 security event
```

当前计划执行 `10 场景 x 3 次 = 30`：

- 实际执行 27 条；
- 27/27 通过；
- 3 条符号链接逃逸测试跳过，因为当前 Windows 账户没有创建 symlink 的权限。

跳过不是通过。因此准确表述应是：

> Windows 环境实际执行的安全回归样本 27/27 通过，另有 3 条 symlink escape 样本因系统权限跳过。

不能写成“30/30 安全测试通过”。

### 5.6 上下文压缩

**要回答的问题：** Prompt 变短后，最新任务和重要信息有没有丢失？

当前实验构造一个 `3 x 2 x 2 = 12` 的配置矩阵：

- History：short / medium / long；
- Memory notes：low / high；
- Current request：short / long。

每个配置重复 5 次，共 60 个构造样本。每个样本分别渲染：

- `full`：开启 Context Reduction；
- `no_context_reduction`：关闭 Context Reduction。

压缩率：

```text
compression_ratio =
    (raw_prompt_chars - full_prompt_chars)
    / raw_prompt_chars
```

当前结果：

- 未压缩平均 7675.33 chars；
- 压缩后平均 6173.67 chars；
- 平均压缩率 16.30%；
- 最大压缩率 32.16%；
- 最新用户请求保留率 100%。

当前实验的局限：

- 统计单位是字符，不是模型 tokenizer 的 token；
- “最新请求保留”只检查 Prompt 结尾是否完整存在；
- 没有让真实模型回答复杂问题来验证压缩前后语义质量；
- 5 次重复使用相同确定性构造，所以主要用于防止代码回归，不代表随机置信度。

### 5.7 Working Memory

**要回答的问题：** 已读过的信息能否被复用，避免重复调用工具？

当前有 12 种任务类型，覆盖：

- fact lookup；
- edit dependency；
- history reference。

每个任务运行 5 次，每个变体 60 条样本：

- `memory_on`：正常使用 Working Memory；
- `memory_off`：关闭 Memory 和 Relevant Memory；
- `memory_irrelevant`：只注入无关记忆。

实验先让 Agent 读取文件并记住事实，再提出依赖该事实的追问：

- 如果 Prompt 中包含正确 Memory，脚本模型直接回答；
- 如果没有正确 Memory，模型再次调用 `read_file` 后回答。

当前结果：

| 变体 | 重复读取 | 平均工具步数 | 平均模型轮数 | 正确率 |
|---|---:|---:|---:|---:|
| memory on | 0 | 0.0 | 1.0 | 100% |
| memory off | 60 | 1.0 | 2.0 | 100% |
| irrelevant memory | 60 | 1.0 | 2.0 | 100% |

它说明当前 Memory 能减少重复工作，同时没有降低这组固定任务的正确率。它尚未证明记忆在大规模真实仓库中始终准确，也没有充分测量“错误记忆误导模型”的风险。

### 5.8 可观测性和持久化

每次 `ask()` 会生成一个独立 run 目录：

```text
.jaxen/runs/<run_id>/
  run_state.json
  trace.jsonl
  report.json
```

- `run_state.json`：这次运行当前/最终状态，只保留最新快照；
- `trace.jsonl`：按时间追加的事件链，用于定位每一步发生了什么；
- `report.json`：最终答案、停止原因、步骤数、Prompt 元数据等汇总。

评测数据不是从终端输出里“看起来像成功”得出的，而是从这些结构化工件、最终文件和 verifier 共同得到。

## 6. 当前结果总表

| 测评层 | 样本 | 当前结果 | 能证明什么 |
|---|---:|---:|---|
| Pytest | 101 tests | 101/101 passed | 当前模块和集成契约没有已知测试回归 |
| Deterministic Harness | 14 tasks | 14/14 strict pass | 固定 Agent Runtime 回归链稳定 |
| Context Ablation | 60 samples | 平均 -16.30%，最大 -32.16%，请求保留 100% | 当前裁切策略能缩短构造 Prompt 且保留最新请求 |
| Memory Ablation | 每变体 60 samples | 重复读取 60 -> 0，正确率均 100% | 固定场景下 Memory 减少重复读取 |
| Recovery Ablation | 每变体 30 samples | 恢复/重锚/漂移检测 100%，误接受 0% | 恢复状态计算和 Prompt 注入符合预期 |
| Security Regression | 30 planned | 27/27 executed pass，3 skipped | 已执行的边界和权限场景符合预期 |
| DeepSeek Provider | 8 tasks | 8/8 pass，2.25 tool steps，3.25 attempts | `deepseek-chat` 能完成当前小型工具编辑集 |

## 7. 这些数字具体从哪里来

### 7.1 代码入口

- 任务加载、隔离执行、verifier 和严格通过规则：
  `jaxen/evaluation/evaluator.py`
- Context、Memory、Recovery、Security 指标：
  `jaxen/evaluation/deterministic_suites.py`
- 离线总入口：
  `scripts/eval/run_fixed_benchmark.py`
- 真实 Provider 入口：
  `scripts/run_real_provider_benchmark.py（2026-10-30 已删除）`
- Agent 步数和停止状态：
  `jaxen/run_state.py`
- Trace、RunState、Report 落盘：
  `jaxen/run_store.py`
- 主循环中的事件记录：
  `jaxen/agent_loop.py`

### 7.2 原始结果

- `artifacts/harness-regression-v2.json`
- `artifacts/context-ablation-v2.json`
- `artifacts/memory-ablation-v2.json`
- `artifacts/recovery-ablation-v2.json`
- `artifacts/security-regression-v1.json`
- `artifacts/real-provider-benchmark-v1.json`
- `artifacts/providers/deepseek-benchmark.json`

如果汇总报告和 JSON 不一致，应以对应的原始 artifact 和当次运行环境为准，并重新生成报告。

### 7.3 为什么结果可复现

确定性 Harness 还记录了：

- Git commit 和 branch；
- fixture 内容的 SHA-256 snapshot；
- 模型名称和版本；
- temperature、top-p、max tokens；
- timezone 和 locale；
- 每条任务的 verifier 输出和 artifact digest。

这些字段帮助回答“这次结果到底基于哪份代码和哪组输入”。

## 8. 新手怎样人工测评

人工测评不是随便和 Agent 聊几句，而是执行固定任务卡并留下证据。

### 8.1 先准备安全环境

1. 使用临时仓库或专门复制出来的测试仓库。
2. 不放真实 API Secret、SSH Key、生产配置和私人文件。
3. 先用 `--approval ask` 或 `--approval never`。
4. 任务开始前记录 Git 状态或文件快照。
5. 设置最大步骤、最大时间和允许工具。
6. 一个任务一个干净工作区，失败后不要手工修复再继续计分。

### 8.2 每条人工任务先写任务卡

```markdown
任务 ID：
任务类别：
初始仓库/commit：
用户 Prompt：
预期修改：
必须通过的测试：
允许工具：
禁止行为：
工具步数预算：
时间预算：
重复次数：
```

### 8.3 执行时观察什么

- Agent 是否先检查相关文件，而不是盲改；
- 是否选择了合适的工具；
- 参数是否合法，路径是否准确；
- 工具错误是否被模型看到并修正；
- 是否修改了需求之外的文件；
- 是否运行必要测试；
- 最终回答是否与实际修改一致；
- 是否正常停止；
- `.jaxen/runs/<run_id>/` 是否留下完整证据。

### 8.4 执行后怎样判定

先做机器可验证项：

- Git diff 是否只包含预期改动；
- 自动测试是否通过；
- 目标文件内容是否满足断言；
- 未允许的文件是否未改变；
- trace 中是否出现权限越界或异常停止。

再做人工质量项：

| 维度 | 0 分 | 1 分 | 2 分 |
|---|---|---|---|
| 正确性 | 未完成或结果错误 | 部分完成 | 完整满足需求 |
| 修改范围 | 大量无关改动 | 有少量多余改动 | 最小且准确 |
| 工具选择 | 明显错误或危险 | 能完成但低效 | 合理且高效 |
| 错误恢复 | 出错后停止/循环 | 恢复但代价较高 | 快速定位并恢复 |
| 测试验证 | 未验证 | 验证不完整 | 运行了充分验证 |
| 最终说明 | 与事实不符 | 信息不完整 | 准确说明改动和验证 |

严格任务完成率仍建议使用 0/1：

- 只有所有硬条件满足才记为成功；
- 人工 0-2 分用于分析质量，不要替代严格成功率。

### 8.5 第一轮人工评估集建议

至少覆盖下面这些类别，每类准备 2-3 条任务：

1. 读取并解释代码，不修改文件。
2. 单文件精确修改。
3. 多文件一致性修改。
4. 修复一个有失败测试的真实 Bug。
5. 新增小功能并补测试。
6. 从报错日志定位问题。
7. 面对模糊需求先澄清或保守处理。
8. 工具参数错误后的恢复。
9. 长对话后引用早期约束。
10. session 重启后继续任务。
11. 越界路径、只读写入和危险 Shell。
12. 不可能任务，检查 Agent 是否诚实停止。

第一轮不要追求任务很多，先确保每条任务都有明确初始状态和独立 verifier。

## 9. 怎样运行自动化测评

### 9.1 日常快速检查

```powershell
python -m pytest -q
python scripts/eval/run_fixed_benchmark.py --quick
```

`--quick` 只把专项实验重复一次，适合开发中快速发现明显回归。

### 9.2 发布前完整离线检查

```powershell
python -m pytest -q
python scripts/eval/run_fixed_benchmark.py
```

它会重新生成确定性 Harness、Context、Memory、Recovery、Security artifact 和核心报告。

### 9.3 真实模型检查

```powershell
python -m scripts.run_real_provider_benchmark --max-new-tokens 512
```

注意：

- 需要 `.env` 中存在对应 API Key；
- 会产生真实 API 费用；
- 网络、限流和 Provider 波动都可能影响结果；
- 真实模型存在随机性，正式统计时每条任务应重复 3-5 次；
- 不要用一次 8/8 代替长期回归趋势。

### 9.4 查看失败

先查看对应 artifact 中失败行：

```powershell
$data = Get-Content -Raw artifacts\harness-regression-v2.json | ConvertFrom-Json
$data.rows | Where-Object { -not $_.passed } |
    Select-Object id, category, failure_category, stop_reason, tool_steps, attempts
```

然后按失败类型定位：

- `missing_artifact`：预期文件没生成；
- `budget_exceeded`：工具步骤超预算；
- `verifier_failed`：产物存在但内容/测试错误；
- `failure_stop_reason`：文件可能正确，但 Agent 没有正常结束；
- `model_error`：Provider 请求或响应解析失败；
- `retry_limit_reached`：模型持续输出无法解析的内容。

最后再读该 run 的 `trace.jsonl`，按事件顺序找第一处偏差，而不是只看最终错误。

## 10. 怎样给自动评估集加任务

1. 准备最小 fixture，固定初始文件内容。
2. 写一个只描述目标、不泄露答案步骤的 Prompt。
3. 明确允许工具和合理步数预算。
4. 写独立 verifier，优先检查行为结果而不是输出文案。
5. 给任务标记类别和难度。
6. 先人工运行 verifier，确认“正确结果必过、错误结果必失败”。
7. 确定性 Harness 任务还要提供固定模型输出。
8. 运行该任务多次，确认不会被前一任务污染。
9. 再加入完整回归集。

一个好的 verifier 应同时防止：

- 漏改；
- 改错位置；
- 只在最终回答中声称完成；
- 删除测试绕过失败；
- 修改无关文件投机通过；
- 在工作区外产生副作用。

## 11. 推荐的回归门禁

下面是下一阶段可以采用的门禁，不是当前已经全部实现的指标：

```text
Pytest pass rate                    = 100%
Deterministic strict pass rate      = 100%
Deterministic verifier pass rate    = 100%
Security executed pass rate         = 100%
Invalid resume false accept rate    = 0%
Current request preservation rate   = 100%
真实模型任务完成率                  不低于已确认 baseline
真实模型 P95 tool steps             不高于预算
未审批高风险操作次数                = 0
工作区外写入次数                    = 0
```

CI 中应把确定性测试作为硬门禁。真实 Provider 测试有费用和波动，可以定时执行或在发布前执行，并使用趋势和置信区间判断，而不是每次要求绝对 100%。

## 12. 当前评测最需要补强的地方

### 12.1 任务规模和真实性不足

当前 Harness 只有两个很小的 fixture，真实 Provider 只有 8 个 read/patch 文件编辑任务。尚未充分覆盖：

- Python 代码理解和 Bug 修复；
- 新增测试；
- 多模块重构；
- Shell、搜索、依赖安装和 Git 工作流；
- 大型仓库定位；
- 模糊需求和需求冲突；
- 后台任务、Cron、Project Task 的端到端协作。

### 12.2 FakeModelClient 不测模型智能

脚本输出是预先写好的，它非常适合测 Runtime 回归，但不会暴露真实模型的规划失败、格式漂移和随机性。14/14 应叫“Runtime regression pass”，不能叫“AI 编程准确率 100%”。

### 12.3 重复次数不等于统计独立样本

Context、Memory、Recovery 使用确定性构造。相同输入重复 5 次主要用于发现状态污染和非确定性 Bug，不会自然增加对真实世界能力的统计信心。

### 12.4 真实模型样本太少

DeepSeek 8/8 是积极信号，但样本量仍小。即使把它当作随机样本，8/8 的 95% Wilson 区间下界也只有约 67.6%。这说明“观察到 100%”和“真实成功率接近 100%”不是一回事。

### 12.5 还缺少质量、成本和延迟

当前 Provider 汇总主要记录通过率、平均步骤和模型轮数。后续应增加：

- 平均/P50/P95 总耗时；
- 首次有效工具调用耗时；
- 输入/输出 token；
- 单任务 API 成本；
- 无关 diff 行数；
- 测试覆盖变化；
- 人工代码质量评分。

### 12.6 安全攻击面还不够完整

应继续增加：

- Prompt Injection 诱导读取 Secret；
- Shell 命令拼接与转义；
- 隐藏文件、绝对路径、UNC 路径和 junction；
- 超大输出和资源耗尽；
- 子 Agent 权限继承；
- 后台线程和并发写入；
- Secret 在 trace/report/final answer 中的脱敏。

同时要在具备 symlink 权限的 Linux CI 中补跑当前跳过的 3 条样本。

## 13. 建议的下一版评估路线

### P0：把现有结果变成稳定回归门禁

- 保持 101 个测试和 14 条 Harness 全通过；
- 修复核心报告在不同终端中的编码显示问题；
- CI 自动保存 JSON artifact；
- 失败时保留对应临时工作区和 trace。

### P1：扩展到真实 Coding 任务

- 建立 30-50 条小型确定性任务；
- 至少包含 10 条 Python Bug Fix；
- 每条任务用测试命令作为 verifier；
- 记录无关 diff、测试通过率和步骤预算。

### P2：真实模型重复评估

- DeepSeek 每条任务重复 3-5 次；
- 固定模型版本、temperature 和 Prompt 版本；
- 报告成功率、均值、P95 和置信区间；
- 加一个 baseline，例如关闭 Memory 或使用简化 Loop。

### P3：人工质量与红队

- 盲评代码质量和最终说明；
- 建立危险操作、Prompt Injection、Secret 泄露测试；
- 专门测试失败恢复、断点续跑和并发状态一致性。

## 14. 你应该怎样理解“我是否掌握了这个 Agent”

当你能独立完成下面这些事情时，就算真正掌握了测评：

1. 能解释 `attempts` 和 `tool_steps` 的区别。
2. 能说明为什么模型自称完成不能算成功。
3. 能为一个新任务写 fixture、budget 和 verifier。
4. 能从 `trace.jsonl` 找到首次失败点。
5. 能区分 Runtime 回归数据和真实模型能力数据。
6. 能解释为什么 8/8 仍然不能证明通用成功率 100%。
7. 能人工检查 diff、测试、权限和最终回答的一致性。
8. 能在新增功能后设计至少一个正常、一个错误、一个安全边界场景。
9. 能准确表述 skipped、failed 和 passed，不把跳过算通过。
10. 能根据失败类别决定是修模型 Prompt、Agent Loop、工具还是 Context。

最核心的思维是：

> Agent 测评不是问“它看起来聪不聪明”，而是给定可复现的初始状态、明确的任务和独立验证器，观察它能否在预算与权限边界内稳定地产生正确结果。

