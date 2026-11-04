# 新中等题 10 道（没见过的题，deepseek-flash，2026-11-02）

## 结论

**8/10 resolved**。这批题是在改规则之后才抽的（种子 20261103，排除之前用过的所有题），agent 之前完全没见过。
2 道失败：1 道是"在表面修、没找根因"，1 道是 **harness 健壮性问题**（模型返回空响应，整道题直接失败）。

| 项目 | 数值 |
|---|---|
| 题目 | SWE-bench Verified `15 min - 1 hour`，`artifacts/swebench/medium10b-sample.json` |
| 仓库 | django 5、xarray 2、sympy 1、astropy 1、matplotlib 1 |
| agent 代码 | 第二版提示规则 + 交付误报修复（修复二）+ 防死循环修复（修复三） |
| 配置 | deepseek-flash、隔离环境、50 步、单题 30 分钟 |
| 输入 token | 599 万 |

对比参考：原来那批中等题（medium10）在隔离环境里是 8/10。两批题不同、各只有 10 道，只能说水平相当，不能说明改进了多少。

## 逐题

| 实例 | 结果 | 说明 |
|---|---|---|
| django-11276 | ✅ | |
| django-12050 | ✅ | |
| django-12406 | ❌ `model_error` | DeepSeek 第 18 轮只返回了思考过程，没有正文也没有工具调用，provider 报"could not extract text"，整个任务失败（已用 65 万 token） |
| django-13028 | ✅ | |
| xarray-6938 | ❌ | 在调用处 `swap_dims` 里给结果加了 `.copy()`，症状消失；根因在 `variable.py`：`IndexVariable.to_index_variable()` 直接返回自身而不是副本。隐藏测试直接检查这个函数 |
| xarray-6744 | ✅ | |
| sympy-12419 | ✅ | |
| django-11999 | ✅ | |
| astropy-8707 | ✅ | |
| matplotlib-24026 | ✅ | |

## 对 harness 的启示

1. **空响应应当可恢复（新发现，建议修）**：thinking 模式下模型偶尔只返回推理、不返回正文或工具调用。现在 provider 直接抛错，
   整个任务失败，前面的进度全部作废。可以改成：空响应时自动重试这次请求（或提示模型"请继续，给出工具调用或最终回答"），
   重试仍失败再报错。这在日常使用中同样会碰到。
   **已修复**：空响应原样重发最多 2 次，见 `docs/architecture/harness-upgrades-20261030.md` 第四轮修复四。
2. **"找根因"仍是主要短板**：xarray-6938 和之前的 sphinx-7462 一样，在出现症状的地方兜底，而没有修产生问题的函数。
   规则里已经写了"找根因"，模型执行得不稳定；更可能靠审查子 agent（"这个修复修的是症状还是根因？"）来补。

第二批 10 道（`medium10c-sample.json`）的镜像已下载，按计划留到 harness 调整后和新难题 10 道一起跑。

明细：`artifacts/swebench/hh-flash-medium20b.{results,predictions,agent-runs}.jsonl`（运行名沿用 medium20b，只包含第一批 10 道）。
