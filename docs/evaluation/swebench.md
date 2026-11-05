# SWE-bench Verified 评测

用官方公开基准测 HH Agent 在真实仓库、真实 issue 上的修复能力。判分完全交给
官方 harness（隐藏测试 FAIL_TO_PASS / PASS_TO_PASS），不使用本项目自己的判分器。

## 流程

```text
SWE-bench/SWE-bench_Verified 实例
→ scripts/swebench/run_hh_predictions.py
   每个实例在它自己的官方镜像里跑 agent（/testbed 已在 base_commit，依赖环境已装好）
   agent 以 JSONL 协议运行，--toolset core，拿 git diff 作为补丁
→ <run>.predictions.jsonl  +  <run>.agent-runs.jsonl（token、请求数、stop_reason、耗时）
   traces/<run-name>/<instance_id>/  （agent 的 trace/report/transcript，诊断用；transcript.jsonl 是逐轮完整对话。2026-11-03 之前的运行在旧布局 traces/<instance_id>/）
→ python -m swebench.harness.run_evaluation（官方判分）
```

## 环境（2026-10-30 实测）

- `swebench==5.0.2` 装在项目内单独的 `.venv-swebench/`（已在 `.gitignore`），不进 agent 本身的依赖；
  重建：`python3.12 -m venv .venv-swebench && .venv-swebench/bin/pip install -r scripts/swebench/requirements.txt`。数据集用新组织名
  `SWE-bench/SWE-bench_Verified`（带 `image` 字段，5.x 必需）。
- 官方镜像只有 amd64。arm64 Mac 上 harness 默认按 arm64 拉取会 404，需先
  `docker pull --platform linux/amd64 <image>`，之后在模拟下运行。脚本会自动这样拉取。
- 单个镜像约 3.8 GB，首次拉取 10~15 分钟；模拟下跑一次测试约 2~5 分钟。
- 镜像内 conda base 是 Python 3.11（agent 用），`envs/testbed` 是仓库自己的环境
  （脚本把它放在 PATH 最前，agent 的 `pytest` 跑的是仓库依赖）。
- **镜像来源**：Docker Hub 官方镜像从新加坡下载只有约 70 KB/s；Epoch AI 在
  `ghcr.io/epoch-research/swe-bench.eval.x86_64.<instance_id>` 重新构建的镜像约 1.2 MB/s，
  20 题都有。流水线 `--image-source epoch` 从 GHCR 拉取后打上官方镜像名，判分流程不变。
  兼容性已用 gold 校验确认（前 6 题官方补丁全部通过，测试约 10 秒，远快于官方镜像在模拟下的速度）。
  **写论文时需注明使用的是 Epoch 重建镜像。**
- **`psf/requests` 不适合本机**：测试访问在线 httpbin.org，官方 gold 补丁在容器里也只过
  7/8 个目标测试。抽样时排除。

## 运行

推荐用流水线：镜像下好一个就跑一个（gold 校验 → agent → 官方判分），结果逐行写入
`artifacts/swebench/<run-name>.results.jsonl`，中断后重跑会跳过已完成的实例：

```bash
.venv/bin/python scripts/swebench/run_pipeline.py --sample artifacts/swebench/easy20-sample.json \
    --run-name hh-flash-easy20 --out-dir artifacts/swebench \
    --swe-python .venv-swebench/bin/python --max-total-tokens 12000000
```

模型接口不可用造成的 `model_error`，流水线会自动处理：删掉这道题的预测、等 `--infra-wait` 秒（默认 120）后重跑，
最多 `--infra-retries` 次（默认 2）；运行记录保留（失败那次的 token 仍计入预算），结果行里记 `infra_retries`。

评测版本：对照实验（审查开/关、同模型基线）统一用 git 标签 `eval-v3`（提交 a34b05b）的代码。为了不受工作区里
正在进行的修改影响，把标签导出成单独的目录，用 `--agent-src` 挂进容器：

```bash
mkdir -p /tmp/hh-eval-v3 && git archive eval-v3 hh_agent_pro | tar -x -C /tmp/hh-eval-v3
```

然后给 `run_pipeline.py` 加 `--agent-src /tmp/hh-eval-v3/hh_agent_pro`。

手工补跑个别实例时注意两点（2026-11-03 踩过）：
- 要把这道题从 `<run-name>.results.jsonl` 和 `.predictions.jsonl` 里都删掉（先备份），`.agent-runs.jsonl` 可以保留。
  只删 results 的话，预测脚本看到已有预测会跳过，流水线拿旧记录再判一次分。
- `--max-total-tokens` 按整个运行名**累计**计算，补跑时上限要按累计值设，否则一开跑就触发预算停止。

抽样：`.venv-swebench/bin/python -I scripts/swebench/make_sample.py --difficulty "15 min - 1 hour" --count 10 --seed 20261102 --out artifacts/swebench/medium10-sample.json`。

也可以分步手动执行：

```bash
SWE=.venv-swebench/bin/python
$SWE -I scripts/swebench/run_hh_predictions.py \
    --instance-ids pytest-dev__pytest-7432 \
    --run-name hh-flash-smoke-20261030 --out-dir artifacts/swebench
$SWE -I -m swebench.harness.run_evaluation -d SWE-bench/SWE-bench_Verified \
    -i pytest-dev__pytest-7432 \
    -p artifacts/swebench/hh-flash-smoke-20261030.predictions.jsonl -id hh-flash-smoke
```

## 定位指标

判分只说明"修没修好"。为了看 agent 是怎么找到要改的代码的，用官方补丁涉及的文件作标准，从 trace 计算定位指标：

```bash
.venv-swebench/bin/python -I scripts/swebench/localization_metrics.py \
    --run-name hh-flash-easy20 --out-dir artifacts/swebench
```

| 指标 | 含义 | easy20 基线 |
|---|---|---|
| found_gold | 读到过正确文件的题数 | 20/20 |
| steps_to_gold（中位） | 第几次工具调用第一次读到正确文件 | 2 |
| files_before_gold（中位） | 读到正确文件前读了几个无关文件 | 0 |
| gold_read_recall | 应改文件中读过的比例 | 1.00 |
| edit_recall / edit_precision | 应改文件中改了的比例 / 改了的文件中该改的比例 | 1.00 / 0.875 |

精确率不到 1 来自 sphinx 镜像自带的 `setup.py`/`tox.ini` 改动（预测脚本已修）和 django-14608 顺手改的文档。
结果写入 `artifacts/swebench/<run-name>.localization.json`。

## 复盘汇总

只读 results、trace 和判分日志，不调用模型：

```bash
python3 scripts/swebench/trace_report.py --run-name hh-flash-medium20d-review --out-dir artifacts/swebench
```

```bash
python3 scripts/swebench/trace_report.py --run-name hh-flash-medium20d-review --out-dir artifacts/swebench --instance sympy__sympy-23413
```

第一条每题一行列出各模块的触发次数（工具失败按错误码、审查结论、交付拦截、截断、压缩、重试），写出 `<run-name>.trace-report.json`；
第二条输出单题时间线：模型和代码版本、逐步工具调用（失败的标出错误码）、关键事件、没通过的隐藏测试、transcript 位置。
要看模型每一轮收到了什么、想了什么，读那道题 run 目录里的 `transcript.jsonl`（按 `kept_messages` + `new_messages` 逐轮还原）。

## 结果

| 运行 | 模型 | 实例 | 结果 | 说明 |
|---|---|---|---|---|
| gold 校验 | 官方补丁 | `pytest-dev__pytest-7432` | resolved | 判分管线正常 |
| gold 校验 | 官方补丁 | `psf__requests-2317` | unresolved | 联网测试失败，环境问题 |
| smoke | deepseek-flash | `pytest-dev__pytest-7432`（<15 min） | **resolved** | 9 次请求、94k 输入 token、120 秒，约 ¥0.2 |
| **easy20 基线** | deepseek-flash | 20 道 `<15 min fix`（Epoch 镜像） | **20/20 resolved** | 225 次请求、295 万输入 token（缓存 79%），约 ¥1.83，见 [2026-11-02-swebench-easy20.md](../results/2026-11-02-swebench-easy20.md)；**环境有泄露，结果无效** |
| **medium10 验证** | deepseek-flash | 10 道 `15 min - 1 hour`（Epoch 镜像，种子 20261102） | **10/10 resolved** | 175 次请求、416 万输入 token（缓存 87%），约 ¥2（估算），见 [2026-11-02-swebench-medium10.md](../results/2026-11-02-swebench-medium10.md)；**环境有泄露，结果无效** |
| **hard10 验证** | deepseek-flash | 10 道 `1-4 hours`（Epoch 镜像，种子 20261102） | **8/10 resolved** | 183 次请求、504 万输入 token（缓存 86%），约 ¥2.5（估算）；发现数据污染迹象，见 [2026-11-02-swebench-hard10.md](../results/2026-11-02-swebench-hard10.md)；**环境有泄露，结果无效** |
| **隔离重跑（有效）** | deepseek-flash | 同样 40 题（简单 20、中等 10、难题 10） | **32/40**（19/20、8/10、5/10） | 隔离环境：无外网、git 历史重建；输入约 1612 万 token，约 ¥10（估算）；失败一半是多文件漏改，见 [2026-11-02-swebench-isolated.md](../results/2026-11-02-swebench-isolated.md) |

smoke 的 agent 自报 `incomplete_delivery`（"code changed without a successful
post-edit test"），但官方判分已解决：它跑的复现命令没被识别为测试。脚本已改为保存
trace，批量跑时可以定位是哪条命令。

单实例只能说明管线跑通，**不代表通过率**。

**评测隔离（2026-11-02 起）**：agent 容器接入没有外网路由的 Docker 内网，唯一出口是只放行模型 API 的代理
（`scripts/swebench/egress_proxy.py`）；agent 开始前 `/testbed` 的 git 历史重建为单个提交。此前的 easy20、medium10、hard10
都在有泄露的环境里运行（镜像含未来的修复提交、容器可联网），**结果无效**，见 [评测环境泄露](../results/2026-11-02-swebench-leakage.md)。
