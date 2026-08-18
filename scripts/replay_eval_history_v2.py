"""Replay the frozen 30-episode DeepSeek history through Schema v2."""

import json
from pathlib import Path

from hh_agent_pro.evaluation.schema_v2 import replay_legacy_artifacts


ROOT = Path(__file__).resolve().parents[1]
INPUTS = (
    ROOT / "artifacts" / "enterprise-eval-20260818" / "reliability-deepseek-trial-1.json",
    ROOT / "artifacts" / "enterprise-eval-20260818" / "reliability-deepseek-trial-2.json",
)
TASKS = ROOT / "benchmarks" / "reliability_tasks_v1.json"
OUTPUT = ROOT / "artifacts" / "eval-v2" / "historical-replay-summary-v2.json"


def main():
    replay = replay_legacy_artifacts(INPUTS, TASKS)
    payload = {
        "schema_version": 2,
        "artifact_type": "historical-replay-summary-v2",
        "source_artifacts": [path.relative_to(ROOT).as_posix() for path in INPUTS],
        "task_suite": TASKS.relative_to(ROOT).as_posix(),
        "summary": replay["summary"],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
