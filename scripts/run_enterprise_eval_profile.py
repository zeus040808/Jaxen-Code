"""Run one NiceEval enterprise profile, retry infra errors, and build gates."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hh_agent_pro.evaluation.enterprise_report_v2 import (
    build_enterprise_summary,
    load_business_feedback,
    load_deterministic_results,
    load_jsonl,
    select_human_reviews,
    supplement_trials_from_junit,
    validate_trial_rows,
    write_enterprise_outputs,
)
from hh_agent_pro.evaluation.evaluator import run_harness_regression_v2
from hh_agent_pro.evaluation.metrics import (
    run_security_regression_v1,
    run_tool_contract_regression_v1,
)
from hh_agent_pro.evaluation.schema_v2 import load_task_suite_v2


EXPERIMENTS = {
    "precheck": ("hh-agent-enterprise-fake", "fake", "scripted-eval"),
    "nightly": ("hh-agent-nightly", "deepseek", "deepseek-v4-pro"),
    "release": ("hh-agent-release", "deepseek", "deepseek-v4-pro"),
}


def _run_niceeval(command, ndjson_path, env):
    ndjson_path.parent.mkdir(parents=True, exist_ok=True)
    with ndjson_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            output.write(line)
            output.flush()
            print(line, end="")
        return process.wait()


def _append_missing_junit_rows(
    trial_path, before_rows, junit_path, tasks, profile, provider, model, run_key, invocation
):
    after_rows = load_jsonl(trial_path) if trial_path.is_file() else []
    before_ids = {str(row.get("record_id")) for row in before_rows}
    delta = [row for row in after_rows if str(row.get("record_id")) not in before_ids]
    junit_rows = supplement_trials_from_junit(
        [], junit_path, tasks, profile, provider, model, run_key
    )
    produced_by_task = Counter(row["task_id"] for row in delta)
    produced_statuses = Counter((row["task_id"], row["trial_status"]) for row in delta)
    junit_by_task = Counter(row["task_id"] for row in junit_rows)
    deficits = {
        task_id: max(0, count - produced_by_task[task_id])
        for task_id, count in junit_by_task.items()
    }
    candidates = []
    for row in junit_rows:
        key = (row["task_id"], row["trial_status"])
        if produced_statuses[key]:
            produced_statuses[key] -= 1
        else:
            candidates.append(row)
    missing = []
    priority = {"infra_error": 0, "grader_error": 1, "skipped": 2, "unsafe": 3}
    candidates.sort(key=lambda row: (priority.get(row["trial_status"], 4), row["task_id"]))
    for ordinal, row in enumerate(candidates, 1):
        if deficits[row["task_id"]] <= 0:
            continue
        deficits[row["task_id"]] -= 1
        row["record_id"] = f"junit:{run_key}:{invocation}:{row['task_id']}:{ordinal}"
        missing.append(row)
    if missing:
        with trial_path.open("a", encoding="utf-8") as handle:
            for row in missing:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return missing


def _run_deterministic(repetitions):
    run_harness_regression_v2()
    run_tool_contract_regression_v1(repetitions=repetitions)
    run_security_regression_v1(repetitions=repetitions)


def _load_baseline(path):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def _receipt_run_ids(paths):
    found = []

    def visit(value):
        if isinstance(value, dict):
            run_ids = value.get("runIds")
            if isinstance(run_ids, list):
                found.extend(str(item) for item in run_ids if item)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                visit(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(dict.fromkeys(found))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=tuple(EXPERIMENTS))
    parser.add_argument("--run-key")
    parser.add_argument("--platform", choices=("linux", "windows"), default="linux")
    parser.add_argument("--baseline")
    parser.add_argument("--business-feedback")
    parser.add_argument("--freeze-baseline")
    parser.add_argument("--infra-retries", type=int, default=2)
    parser.add_argument("--skip-deterministic", action="store_true")
    parser.add_argument("--keep-existing", action="store_true")
    parser.add_argument("--no-view", action="store_true")
    args = parser.parse_args()

    experiment, provider, model = EXPERIMENTS[args.profile]
    run_key = args.run_key or f"{args.profile}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    artifact_dir = Path("artifacts/eval-v2")
    trial_path = artifact_dir / f"trial-results-{args.profile}-v2.jsonl"
    primary_junit = artifact_dir / f"niceeval-{args.profile}-junit.xml"
    if not args.keep_existing:
        trial_path.unlink(missing_ok=True)
        primary_junit.unlink(missing_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_deterministic:
        _run_deterministic(1 if args.profile == "precheck" else 3)

    tasks = load_task_suite_v2("benchmarks/eval_v2/enterprise_tasks_v2.json")["tasks"]
    env = dict(os.environ)
    env["HH_EVAL_RUN_KEY"] = run_key
    base_command = [
        "pnpm",
        "exec",
        "niceeval",
        "exp",
        experiment,
        "--no-early-exit",
        "--json",
    ]
    command = [*base_command, "--junit", str(primary_junit), "--rerun", "all"]
    before = load_jsonl(trial_path) if trial_path.is_file() else []
    ndjson_paths = [artifact_dir / f"niceeval-{args.profile}.ndjson"]
    exit_codes = [_run_niceeval(command, ndjson_paths[0], env)]
    missing = _append_missing_junit_rows(
        trial_path, before, primary_junit, tasks, args.profile, provider, model, run_key, "primary"
    )
    infra_ids = sorted({row["task_id"] for row in missing if row["trial_status"] == "infra_error"})

    for retry in range(1, max(0, args.infra_retries) + 1):
        if not infra_ids:
            break
        retry_junit = artifact_dir / f"niceeval-{args.profile}-infra-retry-{retry}-junit.xml"
        before = load_jsonl(trial_path)
        retry_command = [
            *base_command,
            *[f"enterprise/{task_id}" for task_id in infra_ids],
            "--junit",
            str(retry_junit),
            "--rerun",
        ]
        retry_ndjson = artifact_dir / f"niceeval-{args.profile}-infra-retry-{retry}.ndjson"
        ndjson_paths.append(retry_ndjson)
        exit_codes.append(_run_niceeval(
            retry_command,
            retry_ndjson,
            env,
        ))
        missing = _append_missing_junit_rows(
            trial_path,
            before,
            retry_junit,
            tasks,
            args.profile,
            provider,
            model,
            run_key,
            f"retry-{retry}",
        )
        infra_ids = sorted({row["task_id"] for row in missing if row["trial_status"] == "infra_error"})

    run_ids = _receipt_run_ids(ndjson_paths)
    viewer_path = artifact_dir / f"niceeval-{args.profile}-view"
    if run_ids and not args.no_view:
        view_command = ["pnpm", "exec", "niceeval", "view"]
        for run_id in run_ids:
            view_command.extend(["--run", run_id])
        view_command.extend(["--out", str(viewer_path), "--no-open"])
        subprocess.run(view_command, env=env, check=True)

    rows = validate_trial_rows(
        tasks, load_jsonl(trial_path), args.profile, run_key=run_key
    )
    deterministic = load_deterministic_results(
        "artifacts/deterministic/harness-regression-v2.json",
        "artifacts/deterministic/tool-contract-v1.json",
        "artifacts/deterministic/security-regression-v1.json",
        args.platform,
    )
    summary = build_enterprise_summary(
        tasks,
        rows,
        profile=args.profile,
        provider=provider,
        model=model,
        run_key=run_key,
        deterministic=deterministic,
        baseline=_load_baseline(args.baseline),
        business=load_business_feedback(args.business_feedback),
        evidence={
            "niceeval_experiment": experiment,
            "niceeval_junit": str(primary_junit),
            "niceeval_ndjson": str(artifact_dir / f"niceeval-{args.profile}.ndjson"),
            "trial_results": str(trial_path),
            "niceeval_run_ids": run_ids,
            "niceeval_exit_codes": exit_codes,
            "niceeval_viewer": str(viewer_path) if run_ids and not args.no_view else "",
        },
    )
    reviews = select_human_reviews(tasks, rows)
    outputs = write_enterprise_outputs(artifact_dir, summary, reviews)
    if args.freeze_baseline and args.profile == "nightly" and summary["overall_status"] == "PASS":
        baseline_path = Path(args.freeze_baseline)
        if baseline_path.exists():
            print(f"Frozen baseline already exists; left unchanged: {baseline_path}")
        else:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"Frozen baseline created: {baseline_path}")
    print(json.dumps({"overall_status": summary["overall_status"], "outputs": outputs}, ensure_ascii=False, indent=2))
    return 0 if summary["overall_status"] in {"PASS", "REFERENCE_PRECHECK_PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
