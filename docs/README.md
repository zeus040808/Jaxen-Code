# 文档索引

分三类：**architecture** 讲机制怎么设计的，**guides** 是入门教学，
**evaluation** 是评测体系与历次结果。

---

## architecture — 机制设计

| 文档 | 一句话 |
|---|---|
| [overview.md](architecture/overview.md) | HH Agent Pro 总体架构 |
| [context-management.md](architecture/context-management.md) | 上下文分区预算与全局收缩策略 |
| [memory-and-skills.md](architecture/memory-and-skills.md) | 三层记忆与可复用流程（skill）的设计 |
| [memory-v2-summary.md](architecture/memory-v2-summary.md) | 记忆 v2 改版的动机与结果 |
| [session-and-failure.md](architecture/session-and-failure.md) | Session 数据结构与失败字段语义 |
| [task-state-and-delivery.md](architecture/task-state-and-delivery.md) | 任务进度追踪与交付门禁 |
| [interaction-toolchain.md](architecture/interaction-toolchain.md) | 交互与工具链的修复记录 |
| [codex-cli-streaming.md](architecture/codex-cli-streaming.md) | Codex CLI 后端的流式接入 |

## guides — 入门教学

写给不熟悉 agent 工程的读者，从原理讲起。

| 文档 | 一句话 |
|---|---|
| [context-and-prompt.md](guides/context-and-prompt.md) | 上下文与提示词工程入门 |
| [memory-engineering.md](guides/memory-engineering.md) | 记忆工程入门 |
| [tool-call-engineering.md](guides/tool-call-engineering.md) | 工具调用工程入门 |

## evaluation — 评测体系

| 文档 | 一句话 |
|---|---|
| [overview.md](evaluation/overview.md) | 评测总览 |
| [design-v2.md](evaluation/design-v2.md) | **当前评测设计方案**（五层体系、四维指标、门禁分期） |
| [schema-v2.md](evaluation/schema-v2.md) | 任务 schema 与 8 种 trial 状态定义 |
| [jsonl-protocol-v1.md](evaluation/jsonl-protocol-v1.md) | 非交互机器接口协议 |
| [runbook-v2.md](evaluation/runbook-v2.md) | 评测执行手册 |
| [guide.md](evaluation/guide.md) | 评测使用指南 |
| [textbook.md](evaluation/textbook.md) | 评测方法教学 |
| [reliability-design-v1.md](evaluation/reliability-design-v1.md) | 可靠性评测 v1 设计 |
| [reliability-result-v1.md](evaluation/reliability-result-v1.md) | 可靠性评测 v1 结果 |
| [loop-reliability-fixes.md](evaluation/loop-reliability-fixes.md) | 控制循环可靠性修复记录 |
| [enterprise-eval-20260818.md](evaluation/enterprise-eval-20260818.md) | 2026-08-18 企业评测（历史，结论已被修正） |
| [benchmark-core-report.md](evaluation/benchmark-core-report.md) | 基准核心指标报告（自动生成） |

---

## 最新评测结果

不在 `docs/` 下，而在产物目录：

- [artifacts/eval-v2/FINAL_REPORT_20260819_ZH.md](../artifacts/eval-v2/FINAL_REPORT_20260819_ZH.md) — **总报告**
- [artifacts/eval-v2/PLAN_C_RESULTS_20260819_ZH.md](../artifacts/eval-v2/PLAN_C_RESULTS_20260819_ZH.md) — 安全与 HITL 补测详情
- [artifacts/eval-v2/CORRECTIONS_20260819_ZH.md](../artifacts/eval-v2/CORRECTIONS_20260819_ZH.md) — 对上一轮报告的核验与修正
