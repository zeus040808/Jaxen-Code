#!/bin/zsh
# ① 用测量时的代码（只同步了代理重试修复）补跑 2 道基础设施失败的题；② 用当前代码重跑 12754、23413 验证修复
cd "$(dirname "$0")/../.."
P=(--out-dir artifacts/swebench --swe-python .venv-swebench/bin/python --image-source epoch --pull-workers 2 --review auto)
.venv-swebench/bin/python scripts/swebench/run_pipeline.py --sample artifacts/swebench/medium20d-sample.json \
  --run-name hh-flash-medium20d-review $P --max-total-tokens 20000000 --agent-src "$1" \
  >> artifacts/swebench/hh-flash-medium20d-review.pipeline.log 2>&1
.venv-swebench/bin/python scripts/swebench/run_pipeline.py --sample artifacts/swebench/fixverify2-sample.json \
  --run-name hh-flash-fixverify2 $P --max-total-tokens 3000000 > artifacts/swebench/hh-flash-fixverify2.pipeline.log 2>&1
echo ALL DONE
