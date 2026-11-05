"""上下文压缩模块评测：假模型 + 真 harness，零成本。

    .venv/bin/python scripts/eval/run_context_compaction.py

结果写入 artifacts/deterministic/context-compaction-v1.json；前后比对项目仓库的 git status。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jaxen.evaluation.context_compaction import run_suite  # noqa: E402


def git_status():
    return subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "artifacts/deterministic/context-compaction-v1.json"))
    args = parser.parse_args()

    before = git_status()
    result = run_suite()
    result["summary"]["project_repo_unchanged"] = git_status() == before
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in result["rows"]:
        kind = row.get("kind") if row["part"] == "A" else ",".join(k for k in row["compactions"] if k) or "-"
        print(f"{row['verdict']:5} {row['part']} {row['id']:34} compaction={kind}  facts={row['fact_retention']}")
    print(json.dumps(result["summary"], ensure_ascii=False))
    ok = not result["summary"]["failures"] and result["summary"]["project_repo_unchanged"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
