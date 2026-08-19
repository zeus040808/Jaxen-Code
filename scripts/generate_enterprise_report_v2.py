"""Generate enterprise JSON, JUnit, Chinese report, and human-review CSV."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hh_agent_pro.evaluation.enterprise_report_v2 import (
    annotate_disagreements_from_niceeval_ndjson,
    build_enterprise_summary,
    load_business_feedback,
    load_deterministic_results,
    load_jsonl,
    select_human_reviews,
    supplement_trials_from_junit,
    supplement_trials_from_niceeval_ndjson,
    validate_trial_rows,
    write_enterprise_outputs,
)
from hh_agent_pro.evaluation.schema_v2 import load_task_suite_v2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default="benchmarks/eval_v2/enterprise_tasks_v2.json")
    parser.add_argument("--trials", required=True)
    parser.add_argument("--junit")
    parser.add_argument("--niceeval-ndjson")
    parser.add_argument("--profile", choices=("precheck", "pr", "nightly", "release"), required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--niceeval-run-id")
    parser.add_argument("--niceeval-viewer")
    parser.add_argument("--output-dir", default="artifacts/eval-v2/report-v2")
    parser.add_argument("--baseline")
    parser.add_argument("--business-feedback")
    parser.add_argument("--runtime-artifact", default="artifacts/harness-regression-v2.json")
    parser.add_argument("--tool-artifact", default="artifacts/tool-contract-v1.json")
    parser.add_argument("--security-artifact", default="artifacts/security-regression-v1.json")
    parser.add_argument("--platform", choices=("linux", "windows"), default="windows")
    args = parser.parse_args()

    tasks = load_task_suite_v2(args.tasks)["tasks"]
    rows = load_jsonl(args.trials)
    if args.junit:
        rows = supplement_trials_from_junit(
            rows, args.junit, tasks, args.profile, args.provider, args.model, args.run_key
        )
    if args.niceeval_ndjson:
        rows = supplement_trials_from_niceeval_ndjson(
            rows,
            args.niceeval_ndjson,
            tasks,
            args.profile,
            args.provider,
            args.model,
            args.run_key,
        )
        rows = annotate_disagreements_from_niceeval_ndjson(rows, args.niceeval_ndjson)
    rows = validate_trial_rows(tasks, rows, args.profile, run_key=args.run_key)
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8")) if args.baseline else None
    deterministic = load_deterministic_results(
        args.runtime_artifact, args.tool_artifact, args.security_artifact, args.platform
    )
    business = load_business_feedback(args.business_feedback)
    summary = build_enterprise_summary(
        tasks,
        rows,
        profile=args.profile,
        provider=args.provider,
        model=args.model,
        run_key=args.run_key,
        deterministic=deterministic,
        baseline=baseline,
        business=business,
        evidence={
            "trials": args.trials,
            "niceeval_junit": args.junit or "",
            "niceeval_ndjson": args.niceeval_ndjson or "",
            "niceeval_run_ids": [args.niceeval_run_id] if args.niceeval_run_id else [],
            "niceeval_viewer": args.niceeval_viewer or "",
        },
    )
    reviews = select_human_reviews(tasks, rows)
    paths = write_enterprise_outputs(args.output_dir, summary, reviews)
    print(json.dumps({"overall_status": summary["overall_status"], "outputs": paths}, ensure_ascii=False, indent=2))
    return 0 if summary["overall_status"] in {"PASS", "REFERENCE_PRECHECK_PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
