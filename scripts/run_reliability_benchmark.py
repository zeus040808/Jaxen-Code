"""Run the HH Agent reliability-v1 suite against one configured real provider."""

import argparse
import json
from pathlib import Path

from hh_agent_pro.evaluation.metrics import _make_provider_client, _provider_profile
from hh_agent_pro.evaluation.reliability import load_reliability_suite, run_reliability_suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("deepseek", "gpt", "claude"), default="deepseek")
    parser.add_argument("--suite", default="benchmarks/reliability_tasks_v1.json")
    parser.add_argument("--output", default=None)
    parser.add_argument("--evidence-root", default=None)
    parser.add_argument("--human-review", default=None)
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--task", action="append", dest="tasks", default=[])
    parser.add_argument("--dry-run", action="store_true", help="Validate and describe the suite without API calls.")
    args = parser.parse_args()

    suite = load_reliability_suite(args.suite)
    if args.dry_run:
        categories = {}
        for task in suite["tasks"]:
            categories[task["category"]] = categories.get(task["category"], 0) + 1
        print(json.dumps({"suite_id": suite["suite_id"], "task_count": len(suite["tasks"]), "categories": categories}, indent=2))
        return 0

    profile = _provider_profile(args.provider)
    if profile["status"] != "ready":
        raise RuntimeError(profile["reason"])

    output = Path(args.output or f"artifacts/reliability-v1-{args.provider}.json")
    evidence = Path(args.evidence_root or f"artifacts/reliability-runs/{args.provider}")
    human_review = Path(args.human_review or f"artifacts/human-review-{args.provider}.csv")
    artifact = run_reliability_suite(
        suite_path=args.suite,
        output_path=output,
        evidence_root=evidence,
        human_review_path=human_review,
        model_client_factory=lambda task: _make_provider_client(args.provider),
        provider=args.provider,
        model=profile["model"],
        max_new_tokens=args.max_new_tokens,
        task_ids=args.tasks,
    )
    print(json.dumps(artifact["summary"], indent=2, ensure_ascii=False))
    print(f"\nArtifact: {output}")
    print(f"Evidence: {evidence}")
    print(f"Human review: {human_review}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
