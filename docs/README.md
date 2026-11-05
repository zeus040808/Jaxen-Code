# 文档索引

| 目录 | 放什么 |
|---|---|
| `architecture/` | **现行**机制设计：读代码前先看这里 |
| `evaluation/` | **现行**评测方法、协议与执行手册 |
| `results/` | 各轮评测报告，按日期命名 |
| `learning/` | 教学型长文：从原理讲 agent 评测 |
| `history/` | 已被取代的设计与修复记录，保留演进过程 |

---

## architecture — 机制设计

| 文档 | 一句话 |
|---|---|
| [overview.md](architecture/overview.md) | 总体介绍、运行方式与能力清单 |
| [module-map.md](architecture/module-map.md) | 模块职责与调用链 |
| [harness-upgrades-20261030.md](architecture/harness-upgrades-20261030.md) | 2026-10-30 对照 Claude Code 的 harness 升级（预算、工具、门禁、工具集） |
| [context-engineering.md](architecture/context-engineering.md) | 上下文三层结构、token 预算、轻压缩与摘要压缩 |
| [ui-prototype.md](architecture/ui-prototype.md) | 图形界面（Codex 式布局，AI Elements 组件）：经 CLI 的 JSONL 协议驱动 agent，与 CLI 兼容 |
| [review-subagent.md](architecture/review-subagent.md) | 审查子 agent：交付前由 harness 强制触发的只读审查、`/review` 命令、开关与实测 |
| [memory-and-skills.md](architecture/memory-and-skills.md) | 三层记忆与可复用流程（skill） |
| [session-and-failure.md](architecture/session-and-failure.md) | Session 数据结构与失败字段语义 |
| [task-state-and-delivery.md](architecture/task-state-and-delivery.md) | 任务进度追踪与交付门禁 |

## evaluation — 评测方法

| 文档 | 一句话 |
|---|---|
| [overview.md](evaluation/overview.md) | 评测总览 |
| [design.md](evaluation/design.md) | 评测设计（五层体系、四维指标、门禁分期） |
| [schema.md](evaluation/schema.md) | 任务 schema 与 trial 状态定义 |
| [jsonl-protocol.md](evaluation/jsonl-protocol.md) | 非交互机器接口协议 |
| [runbook.md](evaluation/runbook.md) | 企业评测执行手册 |
| [swebench.md](evaluation/swebench.md) | SWE-bench Verified 评测流程与结果 |

## results — 评测报告

| 文档 | 一句话 |
|---|---|
| [2026-08-19-enterprise-eval.md](results/2026-08-19-enterprise-eval.md) | 74 任务企业评测总报告（harness 升级之前） |
| [2026-11-02-swebench-easy20.md](results/2026-11-02-swebench-easy20.md) | SWE-bench Verified 简单题 20 题基线：20/20 resolved，约 ¥1.83 |
| [2026-11-02-swebench-medium10.md](results/2026-11-02-swebench-medium10.md) | SWE-bench Verified 中等题 10 题验证：10/10 resolved，约 ¥2（估算） |
| [2026-11-02-swebench-hard10.md](results/2026-11-02-swebench-hard10.md) | SWE-bench Verified 难题 10 题验证：8/10 resolved；数据污染迹象与两道失败题分析 |
| [2026-11-02-swebench-leakage.md](results/2026-11-02-swebench-leakage.md) | SWE-bench 评测环境泄露（未来 git 提交、容器联网）的证据、修复与隔离复测；此前三轮结果无效 |
| [2026-11-02-swebench-isolated.md](results/2026-11-02-swebench-isolated.md) | 隔离环境重跑 40 题：32/40（简单 19/20、中等 8/10、难题 5/10），失败归因与过程指标 |
| [2026-11-02-rules-experiment.md](results/2026-11-02-rules-experiment.md) | 提示规则实验：8 道失败题两轮分别救回 2/8、1/8，随机性与规则效果同量级；禁止找答案、交叉检查期望值两条规则有直接证据 |
| [2026-11-02-opus-persistent5.md](results/2026-11-02-opus-persistent5.md) | 换 Opus 4.8 跑 5 道持续失败题：2/5；1 道被防死循环规则挡住（harness 问题），2 道两个模型都做不到 |
| [2026-11-02-swebench-medium10b.md](results/2026-11-02-swebench-medium10b.md) | 没见过的新中等题 10 道（flash）：8/10；1 道失败是模型空响应导致整题失败（harness 健壮性问题） |
| [2026-11-03-review-rerun.md](results/2026-11-03-review-rerun.md) | 新 harness（审查 auto）重跑 2 道失败的中等题：1/2；审查都给 approve、成本占 16%–27%；修了分析脚本取错子 agent trace 的问题 |
| [2026-11-03-swebench-medium10c.md](results/2026-11-03-swebench-medium10c.md) | 第三批新中等题 10 道（审查 auto）：9/10；审查 1 次提出有效问题（sphinx-7985），成本约 14% |
| [2026-11-03-swebench-medium50.md](results/2026-11-03-swebench-medium50.md) | 中等题 50 道统一版本测量（审查 auto）：42/50（补跑基础设施失败题后，全部有效判分）；旧题回归 16/20 持平；模块触发统计、新暴露的 harness 问题和代理重试 bug |
| [benchmark-core-report.md](results/benchmark-core-report.md) | 确定性基准核心指标（`scripts/eval/run_fixed_benchmark.py` 自动生成） |

## learning — 教学

| 文档 | 一句话 |
|---|---|
| [evaluation-textbook.md](learning/evaluation-textbook.md) | 评测方法教学（长文） |
| [evaluation-guide.md](learning/evaluation-guide.md) | 评测使用指南 |

## history — 历史记录

| 文档 | 一句话 |
|---|---|
| [context-management-v1.md](history/context-management-v1.md) | 旧的 8 分区字符配额上下文设计（已被 context-engineering 取代） |
| [codex-cli-streaming.md](history/codex-cli-streaming.md) | 已删除的 Codex CLI 后端 |
| [interaction-toolchain-repair.md](history/interaction-toolchain-repair.md) | Windows 多文件交互与工具链修复 |
| [memory-v2-summary.md](history/memory-v2-summary.md) | 记忆 v2 改版的动机与结果 |
| [loop-reliability-fixes.md](history/loop-reliability-fixes.md) | 控制循环可靠性修复 |
| [reliability-design-v1.md](history/reliability-design-v1.md) | 可靠性评测 v1 设计 |
| [reliability-result-v1.md](history/reliability-result-v1.md) | 可靠性评测 v1 结果 |
| [enterprise-eval-20260818.md](history/enterprise-eval-20260818.md) | 2026-08-18 企业评测（结论已被修正） |
| [2026-10-30-repo-cleanup.md](history/2026-10-30-repo-cleanup.md) | 仓库整理记录：删了什么、挪到哪 |
