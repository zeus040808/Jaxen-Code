"""Run deterministic runtime, tool-contract, and security release gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hh_agent_pro.evaluation.evaluator import run_harness_regression_v2
from hh_agent_pro.evaluation.metrics import (
    run_security_regression_v1,
    run_tool_contract_regression_v1,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--platform", choices=("linux", "windows"), default="windows")
    args = parser.parse_args()

    runtime = run_harness_regression_v2()["summary"]
    tool = run_tool_contract_regression_v1(repetitions=args.repetitions)
    security = run_security_regression_v1(repetitions=args.repetitions)
    authoritative_security = args.platform == "linux"
    summary = {
        "platform": args.platform,
        "runtime_pass_rate": runtime["pass_rate"],
        "tool_contract_pass_rate": tool["pass_rate"],
        "security_pass_rate": security["pass_rate"],
        "security_skipped_runs": security["skipped_runs"],
        "security_authoritative": authoritative_security,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    passed = runtime["pass_rate"] == tool["pass_rate"] == security["pass_rate"] == 1.0
    if authoritative_security:
        passed = passed and security["skipped_runs"] == 0
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
