"""SWE-rebench 判分：官方 swebench harness 不认识这些仓库，按同样的规则在任务镜像里自己判。

每个实例：
  1. 启动任务镜像（/testbed 已在 base_commit，依赖装好）；
  2. 打上要判的补丁（agent 的，或 --gold 时用官方补丁）；
  3. 把官方测试补丁涉及的文件恢复原样，再打上测试补丁（agent 改过测试也不影响判分）；
  4. 跑数据集给的 test_cmd，用 swebench 自带的 pytest 日志解析器读结果；
  5. FAIL_TO_PASS 和 PASS_TO_PASS 全部通过才算 resolved。

    .venv-swebench/bin/python -I scripts/swebench/rebench_grade.py --split 2026_03 \\
        --predictions artifacts/swebench/hh-flash-rebench5.predictions.jsonl --out <results.jsonl>
    # 校验判分本身：--gold --instance-ids ...（官方补丁必须判为通过）
"""

import argparse
import json
import re
import subprocess
from pathlib import Path

DATASET = "nebius/SWE-rebench-leaderboard"
PASSING = {"PASSED", "XFAIL"}


def docker(*args, input=None, timeout=None):
    return subprocess.run(["docker", *args], input=input, capture_output=True, text=True, timeout=timeout)


def apply(container, patch, label):
    if not patch.strip():
        return f"{label}: empty patch"
    result = docker("exec", "-i", "-w", "/testbed", container, "git", "apply", "--whitespace=nowarn", "-", input=patch)
    return "" if result.returncode == 0 else f"{label}: git apply failed: {result.stderr.strip()[:300]}"


def grade(instance, patch, timeout):
    from swebench.harness.log_parsers.python import parse_log_pytest

    iid = instance["instance_id"]
    container = f"hh-grade-{iid.lower().replace('__', '-')}"[:60]
    docker("rm", "-f", container)
    row = {"instance_id": iid}
    # --pull never：本地没有镜像（如构建失败）时立即报错，而不是去 Docker Hub 拉取——那边连不上会卡住整条流水线。
    started = docker("run", "-d", "--pull", "never", "--platform", "linux/amd64", "--name", container,
                     instance["docker_image"], "sleep", "infinity")
    if started.returncode != 0:
        row.update(resolved=False, error=f"image unavailable: {started.stderr.strip()[:200]}")
        return row
    try:
        error = apply(container, patch, "model patch")
        if not error:
            test_files = re.findall(r"^diff --git a/(\S+)", instance["test_patch"], re.M)
            # 新增的测试文件在 HEAD 里不存在，checkout 会报错，忽略即可。
            for path in test_files:
                docker("exec", "-w", "/testbed", container, "git", "checkout", "HEAD", "--", path)
            error = apply(container, instance["test_patch"], "test patch")
        if error:
            row.update(resolved=False, error=error)
            return row
        command = ("source /opt/miniconda3/bin/activate testbed 2>/dev/null || true; "
                   f"cd /testbed && {instance['install_config']['test_cmd']}")
        log = docker("exec", container, "bash", "-lc", command, timeout=timeout).stdout
        status = parse_log_pytest(log, None)
        failed = [name for name in [*instance["FAIL_TO_PASS"], *instance["PASS_TO_PASS"]]
                  if status.get(name) not in PASSING]
        row.update(resolved=not failed, failed_tests=failed[:20], tests_seen=len(status),
                   fail_to_pass=len(instance["FAIL_TO_PASS"]), pass_to_pass=len(instance["PASS_TO_PASS"]))
        if not status:
            row["log_tail"] = log[-1500:]
        return row
    except subprocess.TimeoutExpired:
        row.update(resolved=False, error="test command timed out")
        return row
    finally:
        docker("rm", "-f", container)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", required=True)
    parser.add_argument("--predictions", help="predictions.jsonl to grade")
    parser.add_argument("--gold", action="store_true", help="grade the official patches instead")
    parser.add_argument("--instance-ids", nargs="*", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    from datasets import load_dataset

    instances = {x["instance_id"]: x for x in load_dataset(DATASET, split=args.split)}
    if args.gold:
        patches = {iid: instances[iid]["patch"] for iid in args.instance_ids}
    else:
        rows = [json.loads(line) for line in Path(args.predictions).read_text(encoding="utf-8").splitlines() if line.strip()]
        patches = {row["instance_id"]: row["model_patch"] for row in rows
                   if not args.instance_ids or row["instance_id"] in args.instance_ids}

    with Path(args.out).open("a", encoding="utf-8") as handle:
        for iid, patch in patches.items():
            row = {"mode": "gold" if args.gold else "model", **grade(instances[iid], patch, args.timeout)}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"{iid}: {'resolved' if row['resolved'] else 'unresolved'} "
                  f"{row.get('error') or row.get('failed_tests', '')}", flush=True)


if __name__ == "__main__":
    main()
