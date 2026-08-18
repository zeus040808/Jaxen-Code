"""生成 HH Agent Pro memory v1/v2 确定性能力对照。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hh_agent_pro.evaluation.memory_v2 import write_memory_v2_comparison


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/memory-v2-comparison.json")
    args = parser.parse_args()
    result = write_memory_v2_comparison(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
