# HH Agent Pro

一个从零实现的本地 coding agent harness——跑在终端里，作用于本地代码仓库，
并配套一套可复现的企业式评测体系。

学习 + 求职作品集项目。

---

## 这个项目是什么

`hh_agent_pro` 是包在大模型外面的**控制循环**：组装 prompt、解析模型输出、
校验并执行工具、写审计轨迹、管理记忆与检查点，并在合适的时候停下来。

它不是对某个框架的封装——工具系统、上下文预算、记忆分层、断点恢复、
审批门禁全部自研，目的是把 coding agent 的每一个机制讲清楚。

### 核心能力

| 机制 | 说明 |
|---|---|
| **工具系统** | 26 个工具，统一经 `ToolExecutor` 收口：参数校验 → 审批 → 执行 → 裁剪 → 记账 |
| **安全护栏** | 路径锚定在仓库内、只读模式、审批策略、shell 环境变量白名单、密钥自动脱敏 |
| **上下文管理** | 分区预算 + 全局收缩，关键规则与当前请求永不裁剪 |
| **三层记忆** | 会话工作记忆 / 可纠正的长期事实 / SQLite 跨会话归档 |
| **断点恢复** | 检查点带文件新鲜度指纹，工作区漂移可检测并重新锚定 |
| **人机协同** | `needs_user_input` 与审批作为协议状态；审批前不修改工作区 |
| **可观测性** | 逐事件 `trace.jsonl` + 结果 `report.json` + 可恢复 `run_state.json` |
| **机器接口** | 非交互 JSONL 协议，供自动化评测驱动 |

---

## 快速开始

```bash
pip install -r requirements-dev.txt
cp .env.example .env        # 填入 provider API key
python -m hh_agent_pro       # 进入交互式终端
```

一次性提问：

```bash
python -m hh_agent_pro "解释这个仓库的目录结构"
```

常用参数：

```bash
--provider deepseek|openai|anthropic|ollama|codex
--approval ask|auto|never      # 危险工具的审批策略
--read-only                    # 禁止一切写操作
--resume latest                # 恢复最近一次会话
--max-steps 12                 # 单次请求的工具调用上限
```

跑测试：

```bash
python -m pytest -q            # 138 个测试
```

---

## 目录结构

```
hh_agent_pro/            Agent 本体
├── runtime.py           HH 门面：装配全部子系统
├── agent_loop.py        控制循环（感知 → 决策 → 行动 → 记录）
├── tool_executor.py     工具执行的总闸口
├── context_manager.py   prompt 组装与预算控制
├── prompt_prefix.py     稳定前缀（规则 + 工具目录 + 工作区快照）
├── tools.py             工具注册表与实现
├── memory/              三层记忆
├── skills/              可复用流程的存储与自动沉淀
├── services/            工具背后的长期服务（后台任务、定时、项目任务）
├── providers/           模型后端适配器
├── evaluation/          评测判分与指标（不参与 agent 运行）
├── checkpoint.py        检查点与恢复状态
├── task_state.py        任务进度与交付门禁
├── evidence.py          证据账本（区分已验证结论与假设）
└── jsonl_protocol.py    非交互机器接口

evals/ experiments/      NiceEval 评测定义与实验配置
benchmarks/              任务集与测试仓库 fixture
scripts/                 评测执行与报告生成
artifacts/               评测产物（见下）
docs/                    文档（见下）
tests/                   138 个单元测试
```

### artifacts 布局

| 目录 | 内容 |
|---|---|
| `eval-v2/` | 当前权威评测结果与报告 |
| `deterministic/` | 确定性回归产物（每次运行覆盖） |
| `ablations/` | 组件消融实验 |
| `archive/` | 历史评测，仅供追溯 |

---

## 评测体系

评测分五层，判断标准是**这个问题需不需要真实模型才能回答**：

| 层 | 内容 | 频率 | 成本 |
|---|---|---|---|
| T0 | 单元测试（138 个） | 每次提交 | 0 |
| T1 | Harness 一致性回归（假模型，14 个场景） | 每次提交 | 0 |
| T2 | 组件评测（上下文 / 记忆 / 恢复） | 每次 PR | 0 |
| T3 | 端到端能力（真实模型，74 任务 × 3 次） | 每周 | ~¥15 |
| T4 | 长会话与人工介入 | 每月 | 人工 |

关键设计：

- **能力失败与基础设施失败严格分开**——API 欠费不算模型能力差，不进能力分母
- **`pass^k` 优先于 `pass@k`**——高风险任务要求 3 次全过
- **评测自身也有门禁**——任一分类有效样本 < 3 时拒绝输出任何通过率
- **Python 硬判分器保权威**，NiceEval 只做调度与展示，平台可替换

最新结果：`artifacts/eval-v2/FINAL_REPORT_20260819_ZH.md`

---

## 文档

见 [docs/README.md](docs/README.md)。

---

## 已知短板

评测跑出来的真实结论，不粉饰：

- **代码能力 62%**，弱在多文件改动、重构、测试修复
- **范围纪律不足**——agent 会"顺手多改"，超出允许的文件范围
- **抗干扰弱**——陈旧文档/记忆干扰下容易答错
- **安全与会话控制强**——30 次高风险 trial 零违规、零越权写

框架层强、模型层弱。下一步优先修范围纪律（应由框架强制约束写入路径）。
