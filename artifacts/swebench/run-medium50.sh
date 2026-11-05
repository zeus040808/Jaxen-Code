#!/bin/zsh
# 中等题 50 道测量：后台下载新题镜像，同时回归旧 20 道；旧题跑完再跑新 20 道（都用当前 harness，审查 auto）
cd "$(dirname "$0")/../.."
P=(--out-dir artifacts/swebench --swe-python .venv-swebench/bin/python --image-source epoch --pull-workers 2)
.venv-swebench/bin/python scripts/swebench/run_pipeline.py --sample artifacts/swebench/medium20d-sample.json \
  --run-name prefetch-medium20d $P --pull-only > artifacts/swebench/prefetch-medium20d.log 2>&1 &
.venv-swebench/bin/python scripts/swebench/run_pipeline.py --sample artifacts/swebench/medium-regress20-sample.json \
  --run-name hh-flash-medium-regress20-review $P --review auto --max-total-tokens 20000000 \
  > artifacts/swebench/hh-flash-medium-regress20-review.pipeline.log 2>&1
wait
.venv-swebench/bin/python scripts/swebench/run_pipeline.py --sample artifacts/swebench/medium20d-sample.json \
  --run-name hh-flash-medium20d-review $P --review auto --max-total-tokens 20000000 \
  > artifacts/swebench/hh-flash-medium20d-review.pipeline.log 2>&1
echo ALL DONE
