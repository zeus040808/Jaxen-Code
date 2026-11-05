# SWE-bench Verified 简单题基线（2026-11-02）

> ⚠️ **结果无效**：本轮在有泄露的环境里运行（镜像含未来的 git 提交、容器可联网），agent 在部分题里查到并照抄了官方修复，通过率不能作为能力证据。详见 [评测环境泄露](2026-11-02-swebench-leakage.md)。

## 结论

**20 / 20 resolved**（官方 harness 判分），总花费约 **¥1.83**。

| 项目 | 数值 |
|---|---|
| 题目 | SWE-bench Verified `<15 min fix`，固定种子 `20261031` 抽 20 题，排除 `psf/requests`（测试需联网） |
| 仓库分布 | django 13、sphinx 3、sympy / matplotlib / scikit-learn / pytest 各 1 |
| 模型 | DeepSeek `deepseek-flash`，每题 1 次 |
| harness 版本 | 上下文工程重构 + 验证门禁改为提示 + 子 agent 20 步 + 请求退避重试之后（未提交，见 `docs/architecture/harness-upgrades-20261030.md`） |
| 配置 | `--toolset core`，50 步，8192 输出 token，单题 30 分钟 |
| 镜像 | Epoch AI 在 GHCR 重建的镜像（Docker Hub 从新加坡太慢），已用 gold 校验确认 20 题官方补丁全部通过 |
| 判分 | `swebench==5.0.2` 官方 harness |

## 消耗

| 指标 | 合计 | 每题中位 | 每题最大 |
|---|---|---|---|
| 模型请求 | 225 次 | 10 | 28（sphinx-8459） |
| 输入 token | 2,954,230（其中缓存命中 2,331,776，**79%**） | 84,943 | 704,393（django-12209） |
| 输出 token | 76,927 | — | — |
| agent 耗时 | — | 32 秒 | 223 秒（django-12209） |
| 花费 | ¥1.83（余额 16.86 → 15.03） | 约 ¥0.09 | — |

整轮 10:13 开始、10:54 结束（41 分钟，含边测边下载镜像）。

## 逐题

结果明细：`artifacts/swebench/hh-flash-easy20.results.jsonl`；补丁：`hh-flash-easy20.predictions.jsonl`；
agent 运行记录与 trace：`hh-flash-easy20.agent-runs.jsonl`、`traces/`。20 题 stop_reason 全部为
`final_answer_returned`，没有超步数、超时或交付门禁问题。

## 核查与注意事项

1. **sphinx 补丁里的环境改动**：3 道 sphinx 题的补丁带上了 `setup.py`、`tox.ini`。这是镜像构建时环境搭建
   留下的改动（依赖版本锁定、给 tox 加 `-rA`），不是 agent 写的；预测脚本当时按 HEAD 收集 diff 把它们一起收了。
   去掉这两个文件后重新判分，3 题仍全部通过（`hh-flash-easy20-clean.predictions.jsonl`）。脚本已改为
   agent 开始前先把镜像自带改动提交成基线，之后只收 agent 的改动。
2. **范围纪律**：django-14608 除了修复代码，还顺手改了两份文档（release notes、formsets 文档）。不影响判分，
   但属于"多做"，与提示规则"只做被要求的事"不完全一致。
3. **统计口径**：每题只跑 1 次，样本 20。20/20 的 95% 置信区间下限约 83%；需要每题 3 次才能看稳定性。
4. **只代表简单题**：简单题官方补丁中位只改 4 行、97% 只动一个文件；中等题改动约为 2 倍，多文件比例 5 倍。
5. **镜像来源**：使用 Epoch 重建镜像而非官方镜像，论文中需注明。
