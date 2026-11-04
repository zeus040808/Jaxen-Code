# 第三批新中等题 10 道（审查 auto，deepseek-flash，2026-11-03）

## 结论

**9/10 resolved**。这批题（种子 20261104，排除之前用过的所有题）用当前 harness 跑：第二版提示规则 + 交付误报/防死循环/空响应重发修复 +
审查子 agent（`--review auto`）。10 道都改了源代码，所以都触发了审查：**1 次提出问题（sphinx-7985，最终通过），9 次通过**。
审查占全部输入 token 的 **13.8%**。

| 项目 | 数值 |
|---|---|
| 题目 | `artifacts/swebench/medium10c-sample.json`：django 5、sphinx、pytest、matplotlib、xarray、astropy 各 1 |
| 输入 token | 660 万（其中审查 91 万） |
| 空响应重发 | 本批没有触发 |

对比参考（题目都不同、各 10 道，只能大致参考，不能当作审查有效的证据）：

| 批次 | 审查 | 结果 |
|---|---|---|
| medium10（隔离环境） | 无 | 8/10 |
| medium10b（第一批新题） | 无 | 8/10 |
| **medium10c（第三批新题）** | auto | **9/10** |

## 逐题

| 实例 | 结果 | 审查 | 说明 |
|---|---|---|---|
| sphinx-7985 | ✅ | **issues（2 条）** | 见下文 |
| pytest-7236 | ✅ | approve | |
| django-13513 | ❌ | approve | 隐藏测试检查 issue 没提到的边界情况，见下文 |
| django-15037 | ✅ | approve | |
| django-15731 | ✅ | approve | |
| xarray-7233 | ✅ | approve | |
| astropy-14096 | ✅ | approve | |
| django-13670 | ✅ | approve | |
| django-16032 | ✅ | approve | |
| matplotlib-23314 | ✅ | approve | 问题列表里出现了审查子 agent 自己的"未解决工具失败"，是解析 bug，已修（见下文） |

## sphinx-7985：审查很可能起了作用

需求是让 linkcheck 也检查本地链接。审查指出主 agent 第一版补丁的两个问题：
1. 本地链接的结果在记入 `self.broken` / `self.good` 之前就返回了，坏的本地链接不会让 `make linkcheck` 报错退出；
2. 本地链接的检查在 `linkcheck_ignore` 之前，忽略列表对本地链接不起作用。

主 agent 采纳了两条，重新修改、复现、跑测试后交付，通过判分。第 2 条和官方补丁的做法一致（官方对本地链接也先查忽略列表）；
隐藏测试 `test_defaults` 检查的正是坏链接的输出。审查前的那版补丁没有单独保存，无法拿它去跑隐藏测试，
所以"没有审查就过不了"是推测，但审查提出的两点都是真实的缺陷。

## django-13513：隐藏测试超出了 issue

issue 说调试页面没有遵守 `raise ... from None`（PEP 415）。但出题时的代码其实已经部分处理了 `__suppress_context__`；官方补丁顺带重构了
异常链的遍历，隐藏测试 `test_innermost_exception_without_traceback` 检查"最内层异常没有 traceback"这个 issue 里没提到的边界情况。
agent 的修改对 issue 描述的场景是正确的。这类题靠审查也很难救（审查者同样只看到 issue）。

## 顺带修掉：审查结论的解析噪音

matplotlib-23314 的审查子 agent 自己有一次 `search` 失败，系统按惯例在它的回答末尾追加 "Unresolved at delivery:" 清单，
解析器把这几行当成了审查意见（结论是 approve，没有影响结果）。修复：结论为 approve 时问题列表为空；解析问题时遇到 "Unresolved at delivery" 就停止。
回归测试见 `tests/test_review.py::test_parse_review` 的两个新用例。

## 小结

- 审查在真实任务里能提出有效意见（sphinx-7985），主 agent 会采纳；9 次 approve 里没有发现明显漏判。
- 成本约占 14%，比预估的 20%–40% 低。
- 两类题审查救不了：两种修法都合理但隐藏测试锁定其一（xarray-6938）、隐藏测试检查 issue 之外的行为（django-13513）。
- 要定量证明审查的效果，仍需要同一批题的"开/关"对比。

明细：`artifacts/swebench/hh-flash-medium10c-review.{results,predictions,agent-runs}.jsonl`。
