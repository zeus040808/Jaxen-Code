# 新 harness 重跑 2 道失败的中等题（审查 auto，2026-11-03）

## 结论

新中等题第一批（medium10b）没过的 2 道，用当前 harness（第二版规则 + 交付误报/防死循环/空响应重发修复 + 审查子 agent，`--review auto`）各跑 1 次：
**1/2 通过**。这次没有碰到空响应；审查在两道题上都运行了、都给了 approve，**没有改变结果**。

| 实例 | 上次 | 这次 | 审查 | 说明 |
|---|---|---|---|---|
| django-12406 | ❌ 模型空响应，整题失败 | ✅ | approve（12.9 万输入 token，占本题 16%） | 没再碰到空响应，所以重发机制这次没被触发；通过属于正常完成 |
| xarray-6938 | ❌ 在调用处修 | ❌ | approve（15.6 万输入 token，占本题 27%） | 主 agent 仍在 `swap_dims` 里给结果加 `.copy(deep=False)`；审查子 agent 也注意到 `to_index_variable()` 返回自身，但认为调用处复制"修的就是根因" |

合计 136 万输入 token；审查占约 21%（28.5 万）。

## xarray-6938：审查为什么没拦住

这道题有两种修法都能修好用户报告的问题：在调用处复制（agent 的做法），或让 `IndexVariable.to_index_variable()` 返回副本（官方做法）。
隐藏测试 `test_to_index_variable_copy` 直接检查后者，等于把其中一种当成唯一答案。审查者面对的也是"两种都合理"，
判断为通过并不算明显失误。这类"隐藏测试锁定了某一种实现"的题，靠审查很难救。

## 顺带发现并修掉：分析脚本取错了 trace

子 agent（调查/审查）的运行记录和主 agent 存在同一个 `runs/` 目录里。之前的分析（包括 `localization_metrics.py`）都取"最新的一个"，
有了审查以后取到的是审查子 agent 的 trace（例如 xarray-6938 显示 12 次工具调用，实际主 agent 用了 34 次）。

修复：
- `run_started` 事件新增 `depth`（0 = 主 agent，1 = 子 agent）。
- `localization_metrics.py` 只取主 agent 的 run；没有 `depth` 的旧 trace 用 `request_id` 区分（评测里主 agent 是 `swe-<实例>`，子 agent 是自动生成的 `req_…`）。
- 回归测试：`tests/test_review.py::test_child_runs_are_marked_with_depth_so_analysis_can_skip_them`。

## 注意

- 只有 2 道题、各跑 1 次：12406 通过可能是重跑的随机性，不能算审查或重发的功劳。审查的效果要靠更多题目的开/关对比。
- 审查的成本在真实任务上约占 16%–27%，和之前估计的 20%–40% 相符。

明细：`artifacts/swebench/hh-flash-medium10b-failed2-review.{results,predictions,agent-runs,localization}.json(l)`。
