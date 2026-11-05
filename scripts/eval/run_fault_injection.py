"""错误恢复模块的故障注入评测：假模型 + 真 harness，零成本。

    .venv/bin/python scripts/eval/run_fault_injection.py

结果写入 artifacts/deterministic/fault-injection-v1.json。前后各看一次项目仓库的 git status，
评测改动了真实代码就报错（兜底：防止运行器或护栏自己有 bug）。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jaxen.evaluation.fault_injection import run_suite  # noqa: E402


def git_status():
    return subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "artifacts/deterministic/fault-injection-v1.json"))
    args = parser.parse_args()

    before = git_status()
    result = run_suite()
    result["summary"]["project_repo_unchanged"] = git_status() == before
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for row in result["rows"]:
        flag = "KNOWN " if row["known_issue"] and row["verdict"] != "pass" else ""
        print(f"{row['verdict']:15} {flag}{row['layer']:13} {row['id']:40} {row['stop_reason']:24} requests={row['model_requests']}")
    summary = result["summary"]
    print(json.dumps({key: summary[key] for key in summary if key != "by_layer"}, ensure_ascii=False))
    ok = not summary["regular_failures"] and not summary["silent_failures"] and summary["project_repo_unchanged"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
