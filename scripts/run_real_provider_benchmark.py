"""Run the fixed real-provider tool-use benchmark with configured API keys."""

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hh_agent_pro.evaluation.metrics import run_provider_experiments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", default="benchmarks/real_provider_tasks.json")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--output", default="artifacts/real-provider-benchmark-v1.json")
    parser.add_argument("--provider-artifacts", default="artifacts/providers")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="hh-real-provider-") as workspace_root:
        result = run_provider_experiments(
            args.benchmark,
            workspace_root,
            args.provider_artifacts,
            max_new_tokens=args.max_new_tokens,
        )

    payload = {
        "schema_version": 1,
        "artifact_type": "real-provider-benchmark-v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": args.benchmark,
        **result,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nArtifact: {output}")


if __name__ == "__main__":
    main()
