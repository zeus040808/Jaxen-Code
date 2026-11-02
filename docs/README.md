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
