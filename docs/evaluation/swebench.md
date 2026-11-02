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
   traces/<instance_id>/      （agent 的 trace/report，诊断用）
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

## 结果

| 运行 | 模型 | 实例 | 结果 | 说明 |
|---|---|---|---|---|
| gold 校验 | 官方补丁 | `pytest-dev__pytest-7432` | resolved | 判分管线正常 |
| gold 校验 | 官方补丁 | `psf__requests-2317` | unresolved | 联网测试失败，环境问题 |
| smoke | deepseek-flash | `pytest-dev__pytest-7432`（<15 min） | **resolved** | 9 次请求、94k 输入 token、120 秒，约 ¥0.2 |
| **easy20 基线** | deepseek-flash | 20 道 `<15 min fix`（Epoch 镜像） | **20/20 resolved** | 225 次请求、295 万输入 token（缓存 79%），约 ¥1.83，见 [2026-11-02-swebench-easy20.md](../results/2026-11-02-swebench-easy20.md) |

smoke 的 agent 自报 `incomplete_delivery`（"code changed without a successful
post-edit test"），但官方判分已解决：它跑的复现命令没被识别为测试。脚本已改为保存
trace，批量跑时可以定位是哪条命令。

单实例只能说明管线跑通，**不代表通过率**。
