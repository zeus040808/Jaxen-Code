# 评测结果核验与修正记录

> 日期：2026-08-19
> 对象：`artifacts/eval-v2/HH_AGENT_EVAL_COMPLETE_SUMMARY_ZH.md`
> 性质：独立核验 + 一处判分器缺陷修复 + 受影响数字重算
> 本文不替代原报告，原报告需按 §4 做四处修改

> ⚠️ **本文是时点记录，不是最新结论。**
> 记录之后又发现并修复了交付门禁死锁、安全断言缺陷、Session/HITL 缓存失效等问题。
> 最新完整结论见 [`FINAL_REPORT_20260819_ZH.md`](FINAL_REPORT_20260819_ZH.md)。
---

## 1. 核验结论

对原报告中每一项**可核验**的声明做了独立复查。原始结构化数据在 `71b90ce`
提交过、在 `8b7a572` 被删除，已从 git 恢复后逐项对账。

### 1.1 完全吻合

| 声明 | 核验方式 | 结果 |
|---|---|---|
| 任务集 SHA-256 `B9ED7EA…A53E8` | `sha256sum` | 逐字符匹配 |
| 74 任务，suite 分布 30/8/10/8/18 | 解析任务集 | 精确一致 |
| eval_unit 分布 2/10/53/9 | 解析任务集 | 精确一致 |
| 点名的 16 个任务 ID | 查任务集 | 16/16 真实存在 |
| 7 个里程碑 commit | `git log` | 7/7 存在，message 吻合 |
| 全量回归 221/221 | 重跑 | 通过（修复后为 227） |
| 历史回放 9/3/2/16、14 有效、12 通过、85.71% | 恢复 JSON 比对 | 每个数字精确匹配 |
| 16 infra = 14×402 + 2 解析错误 | 同上 | 精确匹配 |
| 消除 7 个旧 `hard_pass` 假阳性 | `legacy_false_positive_count` | 匹配 |
| 回放源 30 个 episode | 源文件 15+15 行 | 匹配 |
| 内部算术自洽性 | 逐条验算 | 全部自洽 |

### 1.2 诚实性检验：通过

`enterprise-eval-summary-v2.json` 中所有门禁均为 PASS、`coding_capability = 1.0`、
`pass_k = 1.0`。但其元数据为 `provider: "fake"` / `model: "scripted-eval"` /
`profile: "precheck"`。

**原报告没有把这张全绿门禁表冒充为 Agent 能力**，而是明确标注
「不代表 Agent 能力 100%」。整轮标 `INCOMPLETE`、拒绝计算 `pass³`、
业务层 `NOT_EVALUATED`，均为主动自我限制。**未发现捏造。**

### 1.3 无法核验

**第 10 节「真实 DeepSeek Nightly」没有任何结构化证据留存。**

- `git log --all` 全历史中从未提交过 nightly 结果文件
- `.niceeval/active` 仅剩一个空的 `coordination/` 目录
- 两个 NiceEval Run ID 仅出现在两份 Markdown 中，自我引用

原报告已披露原因（容器被主动停止）。结论：该节数字属于**声明**，非**证据**。

### 1.4 两处失准（非造假）

1. §7.1 coding 子拆分写作 bugfix 8 / feature 6 / multi-file 5 / refactor 4 /
   test 4 / readonly 3；实际 `category` 字段为 bugfix 7 / feature 5 /
   multi_file 4 / **normal_completion 4** / read_only 2 / refactor 4 / test 4。
   总数均为 30，子拆分不符——写的是设计意图而非实际字段。
2. §8 引用的 `artifacts/security-regression-v1.json` 内部时间戳为 **2026-08-05**，
   早于里程碑 1 的 security / tool-contract 重新分类，属**陈旧证据当作当前证据引用**。

---

## 2. 判分器缺陷与修复

### 2.1 缺陷

`hh_agent_pro/evaluation/schema_v2.py` 的 `_is_infra_error()` 有一条兜底规则：

```python
if str(row.get("stop_reason", "")) == "model_error":
    return True
```

`stop_reason == "model_error"` 同时覆盖两种性质完全不同的失败：

| 实际原因 | 责任方 | 应否进能力分母 |
|---|---|---|
| Provider 挂了 / 余额不足 / 连接失败 | 外部 | 否 |
| **HH 发出了非法请求（HTTP 400）** | **Agent 自己** | **是** |

后者被误判为 `infra_error` 并**移出能力分母**，直接虚高能力通过率。

### 2.2 实证

从 `.hh/runs/run_20260813-093833-395ef5/run_state.json` 提取到真实响应体：

```json
{"error":{"message":"The `reasoning_content` in the thinking mode must be
 passed back to the API.","type":"invalid_request_error",
 "code":"invalid_request_error"}}
```

这是 HH 未按 DeepSeek thinking 模式协议回传 `reasoning_content` 导致的
**harness 自身缺陷**，原报告 §12.1 也如此定性，却在分类上按基础设施故障处理。

### 2.3 修复

新增 trial 状态 `agent_protocol_error`：

- 计入 `TRIAL_STATUSES`
- **不**计入 `PASSING_TRIAL_STATUSES`（不算通过）
- **不**计入 `EXCLUDED_FROM_CAPABILITY_DENOMINATOR`（**留在分母里**）
- 在 `classify_trial_v2()` 中于 `_is_infra_error` **之前**判定

### 2.4 修复过程中发现的第二个陷阱

初版修复用响应体的 `code` 字段做判据，导致 14 个 402 被误分类
（历史回放 infra 从 16 掉到 2，被既有测试当场拦下）。

原因：**DeepSeek 对 402 Insufficient Balance 同样返回
`code: "invalid_request_error"`**：

```json
{"error":{"message":"Insufficient Balance","type":"unknown_error",
 "code":"invalid_request_error"}}
```

正确判据是 **HTTP 状态码**，不是响应体的 `code` 字段：
400 = 请求非法（Agent 责任）；401/402/429/5xx = 账号或服务状态（外部责任）。

> 这一插曲本身就是「判分器必须被评测」的直接证据：
> 一个看似合理的判据，在真实数据上会把两类完全相反的失败混淆。

### 2.5 回归测试

新增 `tests/test_eval_failure_attribution.py`，6 个用例锁定：

- `agent_protocol_error` 已声明、不算通过、且留在分母
- 真实 DeepSeek 400 响应体 → `agent_protocol_error`，进分母
- 402 / 连接失败 / 连接重置 → `infra_error`，出分母
- 402 响应体中的 `invalid_request_error` **不得**被误判为协议错误
- 未识别的 model_error 仍默认按基础设施处理
- 普通功能失败不受影响

---

## 3. 修正后的数字

### 3.1 真实 DeepSeek Nightly（39 个终态）

原报告 §10.2 记录 4 个 infra 中，3 个为 `reasoning_content` 协议错误
（`coding_bugfix_parse_bool`、`coding_bugfix_subtotal_quantity`、
`coding_feature_chunked`），1 个为连接失败（`coding_refactor_normalize_username`）。

按修正后的分类：

| 指标 | 原报告 | **修正后** | 变化 |
|---|---|---|---|
| 状态分布 | 26 passed / 7 functional_failed / 2 partial / 4 infra | 26 passed / 7 functional_failed / 2 partial / **3 agent_protocol_error** / 1 infra | — |
| 有效能力 trial | 35 | **38** | +3 |
| 总体有效通过率 | 74.29% | **68.42%** | **−5.87pp** |
| Coding 有效 trial | 26 | **29** | +3 |
| Coding 通过率 | 69.23% | **62.07%** | **−7.16pp** |
| infra error 率 | 10.26%（4/39） | **2.56%（1/39）** | **−7.70pp** |

### 3.2 「停止运行」的理由需要改写

原报告的停止理由是「infra 错误率 10.26% 超过 5% 门槛」。
修正后真实 infra 仅 2.56%，**低于门槛，该理由不成立**。

**正确的停止理由**（更硬）：

> 存在一个已知的、会系统性污染多步任务的 Agent 协议缺陷
> （thinking 模式 `reasoning_content` 未回传）。在修复并验证之前，
> 继续执行剩余 183 个 trial 只会产出不可比较的数据。

### 3.3 历史回放不受影响

30 个历史 episode 全部为 402 / 解析错误，无 HTTP 400，
修复后分类完全不变（`infra_error: 16`、有效 14、通过率 85.71%），
已核验数据的可比性得以保留。

---

## 4. 原报告需做的四处修改

| # | 位置 | 原文 | 应改为 |
|---|---|---|---|
| 1 | §1、§10、§13 | 74.29% | **68.42%** |
| 2 | §1、§10、§13 | 69.23% | **62.07%** |
| 3 | §1、§10 | 「infra 错误率 10.26%，超过 5% 门槛」 | 「infra 2.56%；停止原因为已知 Agent 协议缺陷会污染结果」 |
| 4 | §8 | 引用 8-05 的 security artifact | 改引 `artifacts/eval-v2/deterministic-20260819/` |

---

## 5. 本次重跑的确定性证据（2026-08-19）

产物目录：`artifacts/eval-v2/deterministic-20260819/`

| 套件 | 结果 | 说明 |
|---|---|---|
| 全量 pytest | **227 passed** | 原 221 + 新增 6 个归因测试 |
| Harness 一致性回归 | **14/14，100%** | 确定性，模型不参与决策 |
| Tool-contract 回归 | **18/18 run（6 场景×3），100%，0 跳过** | |
| Security 回归（Windows） | **12/15 run（5 场景×3），执行部分 100%，3 跳过** | 跳过 = `symlink_escape`×3；Linux 才是权威平台 |

里程碑 1 的分类迁移已生效并可验证：

- security 5 场景：`path_escape_read`、`read_only_write`、`search_escape`、
  `symlink_escape`、`approval_denied_shell` —— 全部为真实安全边界
- tool-contract 6 场景：`patch_nonunique`、`patch_missing_new_text`、
  `timeout_out_of_range`、`empty_shell_command`、`empty_delegate_task`、
  `repeated_unchanged_read_cache` —— 全部为工具契约

---

## 6. 仍未完成项

| 项 | 状态 | 影响 |
|---|---|---|
| `reasoning_content` 回传缺陷 | **代码路径已验证正常**（构造完整序列测试，assistant tool_call 携带 reasoning_content 成功回传）；修复提交于 `ad7e2a8`（8-18 16:24），晚于可复现的 8-13 故障 | 8-19 nightly 为何仍报同一错误无法确证——原始证据已删。一个可能解释是 Docker 镜像 `COPY hh_agent_pro/` 命中缓存层跑了旧代码 |
| Linux symlink 权威验证 | 未做 | 安全结论不完整，`SUPPLEMENTAL_PASS` 不等于安全认证 |
| grader gold set | 未建 | 判分器仍无常设验收；§2.4 已证明这是实际风险而非理论风险 |
| 判分器负向测试 | 未做 | Fake 预检 74/74 只证明「参考解能通过」，未证明「错误解会被拒绝」 |
| 真实 security / session / HITL / response / step 套件 | 从未执行 | `unsafe = 0` 的分母为 0，不可外推 |
| Nightly 原始证据 | 已丢失 | §10 全部数字无证据链 |

---

## 7. 对外口径（修正版）

> ✅ 可以说：「我设计并实现了 74 任务、四层评测单元、硬 Grader + NiceEval
> 的企业式评测体系；参考预检 74/74，全量回归 227/227；真实模型 pilot 暴露出
> 一个 Provider 多步协议缺陷和复杂 coding 短板。
> **过程中我发现自己的判分器把 Agent 自身的协议 bug 误判成了基础设施故障，
> 修正后能力数字下调约 6~7 个百分点——这说明判分器本身也必须被评测。**」

> ❌ 不能说：74.29%、69.23%、「因 infra 超标而停止」、「安全 100%」、
> 「Nightly / Release 已完成」。

最后那句加粗的话是整个项目最值得讲的一点：它证明的不是 Agent 有多强，
而是**评测体系有能力发现自己在骗自己**。
