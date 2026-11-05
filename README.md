# Jaxen

从零实现的本地 coding agent harness：跑在终端里，读写本地代码仓库，自己跑测试、改代码、交付补丁。
另配一套可复现的评测体系，包括官方 SWE-bench Verified 判分和隔离的评测环境。

这是学习兼求职作品集项目，参考了 Claude Code 的设计。工具系统、上下文管理、交付门禁和审查子 agent 都是自己写的，
没有套用 agent 框架。目标是每个机制都能讲清楚原理和取舍。

> 代码包名 `hh_agent_pro` / `hh_agent_ui`、环境变量 `HH_*`、状态目录 `.hh/` 沿用旧名 HH Agent Pro，
> 对外显示的名字（命令行、网页界面）是 **Jaxen**。

## 当前成绩

SWE-bench Verified，模型 DeepSeek V4 Flash（`deepseek-flash`）。判分用官方 harness（隐藏测试），
环境隔离：没有外网，git 历史重建为单个提交。

| 测量 | 结果 | 说明 |
|---|---|---|
| **中等题 50 道**（15 min – 1 hour） | **42/50（84%）** | 95% 置信区间约 72%–92%；一次统一版本测量，花费约 ¥17.5，[报告](docs/results/2026-11-03-swebench-medium50.md) |
| 简单题 20 道（< 15 min） | 19/20 | 隔离环境，[报告](docs/results/2026-11-02-swebench-isolated.md) |
| 难题 10 道（1 – 4 hours） | 5/10 | 隔离环境，同上 |

几点说明：
- 每组都是从 Verified 里抽的样本，不是全量 500 道，题量小，置信区间宽。
- 更早的 20/20、10/10 成绩是在泄露环境里跑的（镜像里有未来的修复提交、容器能联网），**已作废**，见
  [评测环境泄露](docs/results/2026-11-02-swebench-leakage.md)。
- 中等题 8 道失败里：5 道是修法不完整，2 道与隐藏测试的写法有关，1 道是 harness 问题（已修复并复测通过）。

## 架构一览

```text
用户 / 评测脚本 / 网页 UI
        │  (交互终端 或 JSONL 机器协议)
        ▼
CodingAgent ── 持有模型、工具、会话状态
        │
AgentLoop ── 每轮：组装上下文 → 调模型 → 执行工具 → 更新任务状态 → 判断能否交付
   │            │                │                    │
ContextManager  provider         ToolExecutor         TaskState + 审查子 agent
稳定前缀+会话   OpenAI 兼容 /     参数校验→审批→执行    改了代码必须有改后测试；
+末尾提醒；     Anthropic        →裁剪→记账            改动交付前由只读子 agent 审一遍
接近上限才压缩
        │
RunStore ── trace.jsonl（事件）/ transcript.jsonl（逐轮完整对话）/ report.json / run_state.json
```

| 机制 | 做了什么 |
|---|---|
| **工具系统** | 所有工具调用都经过 `ToolExecutor`：参数校验 → 审批 → 执行 → 输出裁剪 → 记账。`core` 工具集只给编码工具，`full` 再加记忆、任务、定时、技能 |
| **安全护栏** | 路径锚定在仓库内；只读模式；审批策略；shell 环境变量白名单；密钥在 trace、报告、终端里自动脱敏 |
| **上下文工程** | 稳定的 system 前缀 + 只追加的会话记录 + 末尾提醒，提高缓存命中（评测中 83%–87%）。token 预算由模型窗口推导，接近上限才做轻压缩或摘要 |
| **交付门禁** | 改了代码却没有改后测试、还有未解决的工具失败，就不允许交付 |
| **审查子 agent** | 改了非测试源码时，交付前由一个只读子 agent 审查一次（`--review auto`） |
| **错误恢复** | 模型请求按错误类型重试；输出被截断且为空时加大输出预算重发；重复失败检测，防止死循环 |
| **记忆与恢复** | 三层记忆（会话 / 长期事实 / SQLite 归档）；检查点带文件指纹，工作区变化能检测出来 |
| **可观测性** | `transcript.jsonl` 记录每轮发给模型的内容、模型原始输出和推理内容；`run_started` 记录模型、配置和代码版本哈希；`trace_report.py` 出批量统计和单题时间线 |

各模块职责见 [模块导览](docs/architecture/module-map.md)。

## 快速开始

需要 Python 3.11+。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # 填入 API key，.env 已被 git 忽略
python -m hh_agent_pro      # 进入交互终端
```

一次性提问：

```bash
python -m hh_agent_pro "解释这个仓库的目录结构"
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--provider deepseek\|openai\|anthropic` | 模型后端，默认 deepseek；`openai` 是任意 OpenAI 兼容端点（配 `--base-url`、`--model`） |
| `--approval ask\|auto\|never` | 危险工具的审批策略 |
| `--read-only` | 禁止一切写操作 |
| `--review off\|auto\|always` | 交付前的审查子 agent，真实模型默认 auto |
| `--toolset core\|full` | 工具集，默认 core |
| `--resume latest` | 恢复最近一次会话 |
| `--jsonl` | 非交互机器协议，供评测和 UI 驱动 |

网页界面（经同一个 JSONL 协议驱动 agent，和命令行共用一个核心）：

```bash
python -m hh_agent_ui --cwd <要操作的仓库> --port 8765
```

测试：

```bash
python -m pytest -q
```

## 评测

| 层 | 内容 | 用不用真实模型 |
|---|---|---|
| 单元测试 | 300+ 个，覆盖工具、门禁、上下文、恢复、协议 | 否 |
| Harness 回归 | 用脚本化的假模型重放固定场景 | 否 |
| 自建任务集 | 74 个任务 × 3 次，分开统计能力失败和基础设施失败 | 是 |
| SWE-bench Verified | 官方镜像 + 官方判分，隔离环境，流水线自动补跑接口故障 | 是 |

SWE-bench 流水线：gold 补丁校验 → 在官方镜像里跑 agent → 官方 harness 判分，每题一行结果，可以中断续跑。
对照实验统一用 git 标签 `eval-v3` 的代码。用法见 [SWE-bench 评测手册](docs/evaluation/swebench.md)。

## 目录结构

```text
hh_agent_pro/        agent 本体
├── agent.py         CodingAgent：装配能力、持有会话
├── agent_loop.py    单次任务的控制循环与停止条件
├── tool_executor.py 工具执行的总闸口
├── context_manager.py / prompt_prefix.py   上下文组装与压缩
├── task_state.py    任务进度与交付门禁
├── review.py / subagent.py                 审查子 agent
├── providers/       OpenAI 兼容、Anthropic、脚本化测试模型
├── memory/ skills/ services/               记忆、技能、后台服务
└── run_store.py     运行工件落盘
hh_agent_ui/         网页界面（服务端 + React 前端）
scripts/swebench/    SWE-bench 流水线、隔离代理、定位指标、trace 复盘
scripts/eval/        自建任务集评测
benchmarks/ evals/   任务集与评测定义
docs/                设计、评测方法、各轮报告
tests/               自动化测试
```

## 已知短板

- **多文件、多处修改容易漏改**：中等题和难题的失败多数属于这一类，主要受模型能力限制。
- **SWE-bench 只能测"根据 issue 修 bug"**：新功能开发、长会话、需要人参与的场景还没有系统评测。
- **审查子 agent 的价值还没量化**：50 道里提出有效问题 3 次，成本约占输入 token 的 15%；关闭审查的对照实验还没跑。
- **没有和同模型的简单 agent 对比**：harness 本身带来多少提升，还要跑 mini-swe-agent 等基线才能回答。

## 文档

[docs/README.md](docs/README.md) 是全部文档的索引：机制设计、评测方法、按日期排列的评测报告。
