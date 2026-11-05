"""SWE-bench 流水线：镜像下好一个就跑一个，不等全部下完。

    下载线程（并行）── 镜像就绪 ──▶ 队列 ──▶ 处理（串行）：
        gold 校验 ─ 官方补丁都过不了 → 记为 env_unusable，跳过
                  └ 通过 → 跑 agent（run_hh_predictions.py）→ 官方判分 → 写一行结果

处理串行是因为 agent 跑测试在 amd64 模拟环境里很吃 CPU；下载是 IO，和处理同时进行。
已有结果的实例会跳过，可以随时中断后续跑。

    python scripts/swebench/run_pipeline.py --sample artifacts/swebench/easy20-sample.json \\
        --run-name hh-flash-easy20 --out-dir artifacts/swebench --swe-python <装了 swebench 的 python>
"""

import argparse
import json
import queue
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = "SWE-bench/SWE-bench_Verified"


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


def image_ready(image):
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


# Epoch AI 重新构建并托管在 GHCR 上的 SWE-bench 镜像；Docker Hub 很慢时可用（新加坡实测快约 16 倍）。
# 兼容性由流水线的 gold 校验把关：官方补丁在镜像里跑不通就会被记为 env_unusable。
IMAGE_SOURCES = {
    "dockerhub": None,
    "epoch": "ghcr.io/epoch-research/swe-bench.eval.x86_64.{instance_id}:latest",
}


def pull(image, instance_id, source):
    if image_ready(image):
        return True
    remote = IMAGE_SOURCES[source]
    remote = remote.format(instance_id=instance_id) if remote else image
    # 官方镜像只有 amd64，默认按宿主架构拉会 404。
    if subprocess.run(["docker", "pull", "--platform", "linux/amd64", remote], capture_output=True).returncode != 0:
        return False
    if remote != image:
        # harness 按官方镜像名查找：打一个同名标签即可（只多一个名字，不多占磁盘）。
        subprocess.run(["docker", "tag", remote, image], capture_output=True)
    return image_ready(image)


def evaluate(args, instance_id, predictions, run_id):
    """调用官方 harness 判一个实例，返回 resolved（True/False），判分本身出错返回 None。"""
    harness_dir = Path(args.out_dir) / "harness"
    harness_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [args.swe_python, "-I", "-m", "swebench.harness.run_evaluation", "-d", DATASET, "-i", instance_id,
         "-p", predictions, "-id", run_id, "--max_workers", "1", "--report_dir", str(harness_dir)],
        cwd=harness_dir, capture_output=True, text=True,
    )
    model = "gold" if predictions == "gold" else args.run_name
    report = harness_dir / f"{model}.{run_id}.json"
    if not report.exists():
        return None
    data = json.loads(report.read_text(encoding="utf-8"))
    if instance_id in data.get("error_ids", []) or instance_id in data.get("infra_failure_ids", []):
        return None  # 判分本身没跑成（镜像、容器、基础设施），不算 agent 的失败
    return instance_id in data.get("resolved_ids", [])


def agent_record(meta_path, instance_id):
    if not meta_path.exists():
        return {}
    rows = [json.loads(line) for line in meta_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return next((row for row in reversed(rows) if row.get("instance_id") == instance_id), {})


# agent 以这些原因结束说明是基础设施问题（模型接口不可用），不是解题失败：等一会儿自动重跑
INFRA_STOP_REASONS = {"model_error"}


def drop_instance(path, instance_id):
    """从 jsonl 里删掉某道题的行。预测脚本见到已有预测会跳过，重跑前必须先删掉。"""
    if not path.exists():
        return
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    kept = [line for line in lines if json.loads(line).get("instance_id") != instance_id]
    path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")


def run_agent(args, instance_id, command):
    """跑 agent；碰到基础设施失败就等 infra_wait 秒、清掉旧记录再跑，最多 infra_retries 次。

    返回 (output, record, retries)。2026-11-03 中等 50 道有 2 道因此要手工补跑 4 次，还踩了"只删 results
    不删 predictions，预测脚本跳过、流水线拿旧记录再判一次分"的坑。
    """
    out_dir = Path(args.out_dir)
    meta_path = out_dir / f"{args.run_name}.agent-runs.jsonl"
    for retries in range(args.infra_retries + 1):
        output = subprocess.run(command, capture_output=True, text=True).stdout
        record = agent_record(meta_path, instance_id)
        if (record.get("stop_reason") not in INFRA_STOP_REASONS or retries == args.infra_retries
                or "stop: token budget reached" in output):
            return output, record, retries
        log(f"{instance_id}: infra failure ({record.get('stop_reason')}) -> retry "
            f"{retries + 1}/{args.infra_retries} in {args.infra_wait}s")
        time.sleep(args.infra_wait)
        # 只删预测：预测脚本靠它判断"做过没有"。运行记录留着——失败那次花的 token 仍计入预算，也留作审计；
        # agent_record 取的是最新一行
        drop_instance(out_dir / f"{args.run_name}.predictions.jsonl", instance_id)


def process(args, instance_id, results_path):
    out_dir = Path(args.out_dir)
    started = time.monotonic()
    row = {"instance_id": instance_id}

    # 运行编号带上运行名：两条流水线并行校验同一道题时，判分容器和报告不会互相覆盖
    gold = evaluate(args, instance_id, "gold", f"gold-{args.run_name}-{instance_id}")
    row["gold_resolved"] = gold
    if not gold:
        row["outcome"] = "env_unusable"
        log(f"{instance_id}: gold patch does not pass here -> skipped")
    else:
        command = [
            args.swe_python, "-I", str(ROOT / "scripts/swebench/run_hh_predictions.py"),
            "--instance-ids", instance_id, "--run-name", args.run_name, "--out-dir", str(out_dir),
            "--model", args.model, "--provider", args.provider,
            "--review", args.review,
        ]
        if args.max_total_tokens:
            command += ["--max-total-tokens", str(args.max_total_tokens)]
        if args.agent_src:
            command += ["--agent-src", args.agent_src]
        output, record, infra_retries = run_agent(args, instance_id, command)
        if infra_retries:
            row["infra_retries"] = infra_retries
        if "stop: token budget reached" in output or not record:
            row["outcome"] = "skipped_budget"
            log(f"{instance_id}: token budget reached -> stopping")
            append(results_path, row)
            return False
        predictions = str(out_dir / f"{args.run_name}.predictions.jsonl")
        resolved = evaluate(args, instance_id, predictions, f"{args.run_name}-{instance_id}")
        row.update(
            outcome="resolved" if resolved else ("grading_error" if resolved is None else "unresolved"),
            agent_status=record.get("status"),
            stop_reason=record.get("stop_reason"),
            usage=record.get("usage"),
            patch_lines=record.get("patch_lines"),
            agent_latency_s=record.get("latency_s"),
        )
        log(f"{instance_id}: {row['outcome']} (stop={row['stop_reason']}, usage={row['usage']})")
    row["pipeline_latency_s"] = round(time.monotonic() - started, 1)
    append(results_path, row)
    return True


def append(path, row):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", required=True, help="JSON with instance_ids and images.")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--swe-python", required=True, help="Python interpreter with swebench and datasets installed.")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--provider", choices=("deepseek", "anthropic"), default="deepseek")
    parser.add_argument("--review", choices=("off", "auto", "always"), default="off",
                        help="交付前审查子 agent：off / auto / always（默认 off，和之前的评测保持一致）")
    parser.add_argument("--pull-workers", type=int, default=2)
    parser.add_argument("--image-source", choices=sorted(IMAGE_SOURCES), default="dockerhub",
                        help="Where to pull instance images from; images are re-tagged with the official name.")
    parser.add_argument("--max-total-tokens", type=int, default=None)
    parser.add_argument("--agent-src", default=None, help="传给 run_hh_predictions.py：挂进容器的 agent 代码目录")
    parser.add_argument("--infra-retries", type=int, default=2,
                        help="agent 因模型接口不可用（model_error）结束时，自动重跑几次（默认 2）")
    parser.add_argument("--infra-wait", type=int, default=120, help="自动重跑前等待的秒数（默认 120）")
    parser.add_argument("--pull-only", action="store_true",
                        help="Only download and tag images, then exit (no gold check, agent, or grading).")
    args = parser.parse_args()
    # 判分时会切到 harness 子目录运行：解释器、输出目录（报告、预测文件）都必须先转成绝对路径，
    # 否则报告会写进嵌套的相对目录，脚本读不到，所有实例都被误判为 env_unusable。
    args.swe_python = str(Path(args.swe_python).expanduser().absolute())
    args.out_dir = str(Path(args.out_dir).expanduser().absolute())

    sample = json.loads(Path(args.sample).read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / f"{args.run_name}.results.jsonl"
    done = set()
    if results_path.exists():
        done = {json.loads(line)["instance_id"] for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    todo = [iid for iid in sample["instance_ids"] if iid not in done]
    log(f"{len(done)} already done, {len(todo)} to go")

    if args.pull_only:
        with ThreadPoolExecutor(max_workers=args.pull_workers) as pool:
            for instance_id, image in sample["images"].items():
                pool.submit(lambda iid=instance_id, img=image: log(
                    f"pulled {iid}" if pull(img, iid, args.image_source) else f"PULL FAILED {iid}"))
        ready_count = sum(image_ready(image) for image in sample["images"].values())
        log(f"images ready: {ready_count}/{len(sample['images'])}")
        return 0

    ready = queue.Queue()

    def fetch(instance_id):
        image = sample["images"][instance_id]
        ok = pull(image, instance_id, args.image_source)
        log(f"pulled {instance_id}" if ok else f"PULL FAILED {instance_id}")
        ready.put((instance_id, ok))

    pool = ThreadPoolExecutor(max_workers=args.pull_workers)
    for instance_id in todo:
        pool.submit(fetch, instance_id)

    for _ in todo:
        instance_id, ok = ready.get()
        if not ok:
            append(results_path, {"instance_id": instance_id, "outcome": "pull_failed"})
            continue
        if not process(args, instance_id, results_path):
            break
    pool.shutdown(wait=False, cancel_futures=True)
    summarize(results_path)


def summarize(results_path):
    rows = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    counts = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    graded = [row for row in rows if row["outcome"] in {"resolved", "unresolved"}]
    tokens = sum(int((row.get("usage") or {}).get("inputTokens", 0) or 0) for row in rows)
    log(f"summary: {counts}")
    if graded:
        resolved = sum(row["outcome"] == "resolved" for row in graded)
        log(f"resolved {resolved}/{len(graded)} graded instances; input tokens {tokens}")


if __name__ == "__main__":
    sys.exit(main())
