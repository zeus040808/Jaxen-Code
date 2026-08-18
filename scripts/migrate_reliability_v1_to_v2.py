"""Regenerate the v2 reliability task file from the frozen v1 source."""

import json
from pathlib import Path

from hh_agent_pro.evaluation.schema_v2 import upgrade_reliability_task_v1


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "benchmarks" / "reliability_tasks_v1.json"
TARGET = ROOT / "benchmarks" / "eval_v2" / "reliability_tasks_v2.json"


def main():
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    payload = {
        "schema_version": 2,
        "suite_id": "hh-reliability-v2",
        "suite_version": 1,
        "source": SOURCE.relative_to(ROOT).as_posix(),
        "tasks": [
            upgrade_reliability_task_v1(task, ROOT)
            for task in source["tasks"]
        ],
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(payload['tasks'])} tasks to {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
