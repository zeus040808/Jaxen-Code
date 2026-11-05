"""Run the reproducible offline HH Agent benchmark suite."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hh_agent_pro.evaluation.evaluator import run_harness_regression_v2
from hh_agent_pro.evaluation.metrics import (
    run_memory_ablation_v2,
    run_recovery_ablation_v2,
    run_security_regression_v1,
    run_tool_contract_regression_v1,
    write_benchmark_core_report,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Use one repetition per ablation instead of the report defaults.",
    )
    args = parser.parse_args()
    repetitions = 1 if args.quick else None

    harness = run_harness_regression_v2()
    memory = run_memory_ablation_v2(**({"repetitions": repetitions} if repetitions else {}))
    recovery = run_recovery_ablation_v2(**({"repetitions": repetitions} if repetitions else {}))
    security = run_security_regression_v1(**({"repetitions": repetitions} if repetitions else {}))
    tool_contract = run_tool_contract_regression_v1(**({"repetitions": repetitions} if repetitions else {}))
    report = write_benchmark_core_report()

    print(
        json.dumps(
            {
                "harness": harness["summary"],
                "memory": memory["variants"],
                "recovery": recovery["variants"]["resume_enabled"]["summary"],
                "security": {
                    "scenario_count": security["scenario_count"],
                    "executed_runs": security["executed_runs"],
                    "skipped_runs": security["skipped_runs"],
                    "pass_rate": security["pass_rate"],
                },
                "tool_contract": {
                    "scenario_count": tool_contract["scenario_count"],
                    "pass_rate": tool_contract["pass_rate"],
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print("\nReport: docs/results/benchmark-core-report.md")
    return report


if __name__ == "__main__":
    main()
