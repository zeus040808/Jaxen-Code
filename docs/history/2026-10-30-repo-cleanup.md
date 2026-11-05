# 2026-10-30 仓库整理记录

目标：只保留有用内容，目录层次清晰。整理前对工作区做了完整备份（含 `.git`、`.hh`）。
分三阶段，本次完成阶段一、二；阶段三（包结构重组、去掉版本号模块名、移除 `HH`
别名）单独进行。

## 删除

| 内容 | 原因 |
|---|---|
| `.tmp/`、`.transcripts/`、`.pytest_cache/`、`.niceeval/`、所有 `__pycache__/` | 本地缓存与旧运行记录（已在 `.gitignore`） |
| `.hh/` | agent 在本仓库的本地会话与运行记录，与项目代码无关 |
| `artifacts/archive/`、`artifacts/reliability-runs/` | 过期评测证据 |
| `artifacts/eval-v2/` 下 CORRECTIONS / PLAN_C / COMPLETE_SUMMARY 三份报告 | 结论已并入总报告或与之重复 |
| `artifacts/eval-v2/p0-history-coding-20261030-junit.xml`、`trial-results-flash-ab-v2.jsonl` | 余额不足导致的残缺数据 / 没有对照组的半程 A/B |
| `scripts/` 下 6 个脚本：`migrate_reliability_v1_to_v2`、`replay_eval_history_v2`、`run_real_provider_benchmark`、`run_reliability_benchmark`、`generate_enterprise_report_v2`、`summarize_trial_results` | 无任何代码、CI、配置引用（一次性迁移或 v1 遗留） |
| `benchmarks/real_provider_tasks.json`、`benchmarks/eval_v2/baseline-manifest.json` | 只被已删脚本使用 / 无引用 |
| `package-lock.json` | 项目用 pnpm，锁文件以 `pnpm-lock.yaml` 为准 |

## 移动与改名

| 原位置 | 新位置 |
|---|---|
| `scripts/build_enterprise_eval_v2.py` | `scripts/eval/build_enterprise_dataset.py` |
| `scripts/run_enterprise_eval_profile.py` | `scripts/eval/run_eval_profile.py` |
| `scripts/run_deterministic_eval_gate.py` | `scripts/eval/run_deterministic_gate.py` |
| `scripts/diagnose_coding_episode.py` | `scripts/eval/diagnose_episode.py` |
| `scripts/run_hh_benchmarks.py` | `scripts/eval/run_fixed_benchmark.py` |
| `scripts/run_memory_v2_comparison.py` | `scripts/eval/run_memory_ablation.py` |
| `scripts/{build_grader_gold_set,summarize_human_review}.py` | `scripts/eval/` 下同名 |
| `scripts/patch_niceeval_windows.py` | `scripts/dev/patch_niceeval_windows.py` |
| `tests/test_hh_agent_pro_*.py`（8 个） | 去掉前缀，如 `test_jsonl_protocol.py`、`test_memory.py` |
| `artifacts/archive/.../reliability-deepseek-trial-{1,2}.json` | `tests/fixtures/legacy_reliability/`（历史回放测试的数据） |
| `artifacts/eval-v2/FINAL_REPORT_20260819_ZH.md` | `docs/results/2026-08-19-enterprise-eval.md` |
| `docs/evaluation/{design-v2,schema-v2,runbook-v2,jsonl-protocol-v1}.md` | 去掉版本号 |
| v1 设计/结果、修复记录、记忆 v2 改版说明 | `docs/history/` |
| `docs/evaluation/{textbook,guide}.md` | `docs/learning/evaluation-{textbook,guide}.md` |
| `docs/evaluation/benchmark-core-report.md` | `docs/results/`（生成路径同步修改） |

## 连带修改

- CI（`.github/workflows/*.yml`）与 `package.json` 中的脚本路径。
- 脚本内定位仓库根目录由 `parents[1]` 改为 `parents[2]`；`run_fixed_benchmark.py`、
  `summarize_human_review.py` 补上原本缺失的 `sys.path` 设置。
- `benchmarks/eval_v2/{enterprise,runtime}_tasks_v2.json` 重新生成：只有 `generated_by`
  字段从旧脚本路径变成新路径，任务内容不变，但文件 SHA 随之变化。
- 文档相对链接按新位置重写，并修好此前已失效的旧文件名链接，`docs/README.md` 重写。
- `overview.md` 删除对不存在的 `hh_coder_agent` 的对比，更新默认步数与测试数。

## 验证

`pytest` 206 passed（唯一失败用例当时误判为读取本机 `.env`，实为代码默认模型已改为 flash，
见第二轮）；确定性回归 runtime /
tool-contract / security 均 100%；`tsc --noEmit` 通过；文档坏链接 0。

## 第二轮：去掉无意义的文件和代码

| 内容 | 处理 |
|---|---|
| `hh_agent_pro/runtime.py` | 只转发 3 个名字，删除；15 个测试改为从 `agent` / `session_store` 导入 |
| 别名 `HH = CodingAgent` | 删除；`HH(...)`、`HH.parse`、`HH.from_session` 全部改为 `CodingAgent` |
| `tests/__init__.py` | 空文件（只有 BOM），唯一作用是让 pytest 把仓库根放进 `sys.path`；改为在 `pyproject.toml` 显式写 `pythonpath = ["."]` 后删除 |
| 35 个从未被调用的函数 | 4 个在 agent 运行时（`note_tool`、`render_memory_text` 两个转发别名，`relative_workspace_path`、`_extract_openai_text_from_sse`），31 个在 `evaluation/`（被删旧脚本留下的实验函数及其辅助函数，迭代 4 轮直到没有新孤儿） |
| 22 处未使用的 import | 按 pyflakes 结果逐条删除 |
| 6 个未使用的常量 | 4 个从未赋值的 `STOP_REASON_*`；`DEFAULT_SECTION_FLOORS`（实际下限由 `budget // 4` 计算，常量只会误导）；`REAL_SECURITY_SCENARIOS` |
| `test_deepseek_v4_pro_is_default…` | 长期失败：代码默认模型已改为 `deepseek-flash`，测试仍断言 pro。改名为 `test_deepseek_flash_is_default…`，并隔离 `.env` 载入的环境变量；帮助文本与文档同步 |

扫描方法：AST 统计每个函数/类/常量在全仓 `.py`/`.ts` 中的出现次数，只出现在定义处即视为未使用；
pyflakes 检查未使用 import。结果：`pytest` 207 passed、0 failed；确定性回归 100%；pyflakes 干净。
