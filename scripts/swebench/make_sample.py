"""按难度用固定种子抽 SWE-bench Verified 样本，写成流水线用的 JSON（instance_ids + 官方镜像名）。

    .venv-swebench/bin/python -I scripts/swebench/make_sample.py \\
        --difficulty "15 min - 1 hour" --count 10 --seed 20261102 \\
        --out artifacts/swebench/medium10-sample.json
"""

import argparse
import json
import random
from pathlib import Path

DATASET = "SWE-bench/SWE-bench_Verified"


def image_name(instance_id):
    # 官方镜像名把 "__" 写成 "_1776_"。
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--difficulty", required=True, help='e.g. "<15 min fix", "15 min - 1 hour"')
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--exclude-repo", action="append", default=["psf/requests"],
                        help="psf/requests 的测试要联网，镜像里跑不通。")
    parser.add_argument("--exclude-sample", action="append", default=[],
                        help="已用过的样本 JSON（可重复）：其中的题不再抽，用来抽互不重叠的验证集。")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from datasets import load_dataset

    used = {iid for path in args.exclude_sample for iid in json.loads(Path(path).read_text(encoding="utf-8"))["instance_ids"]}
    pool = sorted(
        row["instance_id"] for row in load_dataset(DATASET, split="test")
        if row["difficulty"] == args.difficulty and row["repo"] not in args.exclude_repo and row["instance_id"] not in used
    )
    picked = random.Random(args.seed).sample(pool, args.count)
    sample = {
        "seed": args.seed,
        "difficulty": args.difficulty,
        "excluded_repos": args.exclude_repo,
        "excluded_samples": args.exclude_sample,
        "pool_size": len(pool),
        "instance_ids": picked,
        "images": {iid: image_name(iid) for iid in picked},
    }
    Path(args.out).write_text(json.dumps(sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"picked {len(picked)} of {len(pool)}:", *picked, sep="\n  ")


if __name__ == "__main__":
    main()
