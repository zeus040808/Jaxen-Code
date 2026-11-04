# Jaxen

> 原名 HH Agent Pro。对外名字（命令行欢迎界面、`jaxen>` 提示符、`--help`、网页界面）已改为 **Jaxen**；
> 代码里的包名 `hh_agent_pro` / `hh_agent_ui`、环境变量 `HH_*`、状态目录 `.hh/` 和历史文档保持不变，避免影响已有功能与评测记录。

一个从零实现的本地 coding agent harness——跑在终端里，作用于本地代码仓库，
并配套一套可复现的企业式评测体系。

学习 + 求职作品集项目。

---

## 这个项目是什么

`hh_agent_pro` 是本地 coding agent：`CodingAgent` 持有模型、工具和跨请求状态，
`AgentLoop` 控制单次任务从开始到交付的决策循环。上下文、工具、安全与记忆
由各自模块提供，不由模型客户端直接执行。

它不是对某个框架的封装——工具系统、上下文预算、记忆分层、断点恢复、
审批门禁全部自研，目的是把 coding agent 的每一个机制讲清楚。

### 核心能力

| 机制 | 说明 |
|---|---|
| **工具系统** | 26 个工具，统一经 `ToolExecutor` 收口：参数校验 → 审批 → 执行 → 裁剪 → 记账 |
| **安全护栏** | 路径锚定在仓库内、只读模式、审批策略、shell 环境变量白名单、密钥自动脱敏 |
| **上下文工程** | 稳定 system + 只追加会话记录 + 末尾提醒；按模型窗口推导 token 预算，接近上限才轻压缩/摘要压缩 |
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
--provider deepseek|openai     # openai = 任意 OpenAI 兼容端点（需 --base-url、--model）
--approval ask|auto|never      # 危险工具的审批策略
--read-only                    # 禁止一切写操作
--resume latest                # 恢复最近一次会话
--max-steps 50                 # 单次请求的工具调用上限（默认 50）
--toolset core|full            # core 只给编码工具（默认）；full 再加记忆/任务/定时/技能工具
```

跑测试：

```bash
python -m pytest -q            # 运行项目测试
```

---

## 目录结构

```
hh_agent_pro/            Agent 本体
├── agent.py             CodingAgent：装配能力、持有 Session、提供 ask()
├── agent_loop.py        单次任务的控制循环与停止条件
├── tool_executor.py     工具执行的总闸口
├── context_manager.py   上下文组装与压缩
├── prompt_prefix.py     稳定前缀（规则 + 工具目录 + 工作区快照）
├── tools.py             工具注册表与实现
├── memory/              三层记忆
├── skills/              可复用流程的存储与自动沉淀
├── services/            工具背后的长期服务（后台任务、定时、项目任务）
├── providers/           OpenAI 兼容模型客户端 + 测试用脚本化模型
├── evaluation/          评测判分与指标（不参与 agent 运行）
├── checkpoint.py        检查点与恢复状态
├── task_state.py        任务进度与交付门禁
├── evidence.py          证据账本（区分已验证结论与假设）
└── jsonl_protocol.py    非交互机器接口

evals/ experiments/      NiceEval 评测定义与实验配置
benchmarks/              任务集与测试仓库 fixture
scripts/eval/            企业评测：数据集生成、执行、门禁、诊断
scripts/swebench/        SWE-bench 预测生成
scripts/dev/             开发辅助（NiceEval Windows 补丁）
artifacts/               评测产物（见下）
docs/                    文档：architecture / evaluation / results / learning / history
tests/                   自动化测试
```

各模块的完整职责与调用关系见 [模块导览](docs/architecture/module-map.md)。

### artifacts 布局

| 目录 | 内容 |
|---|---|
| `eval-v2/` | 企业评测的 trial 结果（报告见 `docs/results/`） |
| `deterministic/` | 确定性回归产物（每次运行覆盖） |
| `ablations/` | 组件消融实验 |
| `swebench/` | SWE-bench 预测与 agent 运行记录 |

---

## 评测体系

评测分五层，判断标准是**这个问题需不需要真实模型才能回答**：

| 层 | 内容 | 频率 | 成本 |
|---|---|---|---|
| T0 | 单元测试（约 210 个） | 每次提交 | 0 |
| T1 | Harness 一致性回归（假模型，14 个场景） | 每次提交 | 0 |
| T2 | 组件评测（上下文 / 记忆 / 恢复） | 每次 PR | 0 |
| T3 | 端到端能力（真实模型，74 任务 × 3 次） | 每周 | ~¥15 |
| T4 | 长会话与人工介入 | 每月 | 人工 |

关键设计：

- **能力失败与基础设施失败严格分开**——API 欠费不算模型能力差，不进能力分母
- **`pass^k` 优先于 `pass@k`**——高风险任务要求 3 次全过
- **评测自身也有门禁**——任一分类有效样本 < 3 时拒绝输出任何通过率
- **Python 硬判分器保权威**，NiceEval 只做调度与展示，平台可替换

最新结果：[docs/results/2026-08-19-enterprise-eval.md](docs/results/2026-08-19-enterprise-eval.md)

---

## 文档

见 [docs/README.md](docs/README.md)。

---

## 已知短板

评测跑出来的真实结论，不粉饰：

- **代码能力**：自建 fixture 上 DeepSeek V4 Pro 为 27/30（交付门禁死锁修复后单次测量，无方差数据）；
  仍弱在多文件一致性与重构兼容
- **范围纪律不足**——agent 会"顺手多改"，尤其会自己改测试文件。2026-10-30 起由提示规则约束，
  并保留 `write_scope` 运行时护栏
- **抗干扰弱**——陈旧文档/记忆干扰下容易答错
- **安全与会话控制强**——30 次高风险 trial 零违规、零越权写
- **真实仓库能力尚无基线**——SWE-bench Verified 只跑通 1 题冒烟，见
  [docs/evaluation/swebench.md](docs/evaluation/swebench.md)

以上数字来自 2026-08-19 报告，之后的 harness 升级（上下文、预算、工具、门禁）尚未重新测量。
