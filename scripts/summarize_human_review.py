"""Validate and summarize a completed HH reliability human-review CSV."""

import argparse
import json

from hh_agent_pro.evaluation.reliability import summarize_human_review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    args = parser.parse_args()
    print(json.dumps(summarize_human_review(args.csv_path), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
