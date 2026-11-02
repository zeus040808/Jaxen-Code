"""Run HH Agent on SWE-bench instances and write a predictions.jsonl for the official harness.

每个实例在它自己的官方评测镜像里跑 agent：镜像里 /testbed 已检出到 base_commit，
依赖环境（conda env "testbed"）也装好了，agent 能真的跑测试；shell 命令也被隔离在容器里。

流程（每个实例）：
  1. docker run 实例镜像（amd64，在 arm64 Mac 上走模拟），把 hh_agent_pro 只读挂进去；
  2. 容器内用 python>=3.11 以 JSONL 机器协议运行 agent（和 NiceEval 评测同一接口），
     从 turn 输出里拿 token 用量、请求数和 stopReason；
  3. `git diff` 收集补丁（排除 .hh/），写入 predictions.jsonl。

之后用官方 harness 判分：
  python -m swebench.harness.run_evaluation -d SWE-bench/SWE-bench_Verified \
      -p <predictions.jsonl> -i <ids...> -id <run_id>

需要在装了 `datasets` 的 Python 里运行（例如单独的 swebench venv）。
DeepSeek 密钥从项目 .env 读取后只通过环境变量传给容器，不写入任何文件或日志。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROMPT = """You are working in a checkout of the {repo} repository at /testbed.
Resolve the following GitHub issue by editing the repository's source files.

<issue>
{problem}
</issue>

Requirements:
- Make the smallest change to non-test source files that resolves the issue.
- Do not modify existing tests; hidden tests will check the fix.
- The repository's Python environment is on PATH, so `python` and `pytest` run against it.
  You may write a short reproduction script, run it, and delete it before finishing.
- Finish with a short summary of the root cause and the change."""


def load_env_values(names):
    """Read only the named keys from the project .env (CRLF-safe)."""
    values = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in names:
            values[key] = value.strip().strip('"').strip("'")
    return values


def run(cmd, timeout=None, check=True, env=None):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and result.returncode != 0:
        raise RuntimeError(f"{cmd[:4]} failed ({result.returncode}): {result.stderr[-2000:]}")
    return result


def ensure_image(image):
    if run(["docker", "image", "inspect", image], check=False).returncode == 0:
        return
    # 官方镜像只有 amd64；默认按宿主架构拉取会 404，所以显式指定平台。
    run(["docker", "pull", "--platform", "linux/amd64", image], timeout=3600)


def find_agent_python(container):
    """Pick a python>=3.11 inside the image for the agent itself."""
    probe = (
        "for p in /opt/miniconda3/bin/python3 /usr/local/bin/python3 /usr/bin/python3 "
        "/opt/miniconda3/envs/*/bin/python3; do "
        "[ -x \"$p\" ] && $p -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' "
        "2>/dev/null && echo $p && break; done"
    )
    out = run(["docker", "exec", container, "bash", "-lc", probe], check=False).stdout.strip()
    return out.splitlines()[0] if out else ""


def commit_baseline(container):
    """把镜像自带的工作区改动（如 sphinx 镜像里环境搭建时改过的 setup.py、tox.ini）提交成基线。

    之后收集的 diff 只包含 agent 的改动；判分时官方镜像里本来就有这些环境改动，补丁才能干净地打上。
    """
    script = (
        "cd /testbed && git add -A && "
        "git -c user.name=hh -c user.email=hh@local commit -q --allow-empty -m 'hh: environment baseline'"
    )
    run(["docker", "exec", container, "bash", "-lc", script])


def collect_patch(container):
    script = (
        "cd /testbed && git add -A -- . ':(exclude).hh' >/dev/null 2>&1; "
        "git diff --cached --no-color -- . ':(exclude).hh'"
    )
    return run(["docker", "exec", container, "bash", "-lc", script]).stdout


def last_turn(stdout):
    """The JSONL protocol ends each request with one kind=turn line."""
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("kind") == "turn":
            return payload
    return {}


def run_instance(instance, args, secrets):
    iid = instance["instance_id"]
    image = instance["image"]
    container = f"hh-swe-{iid.lower().replace('__', '-')}"[:60]
    ensure_image(image)
    run(["docker", "rm", "-f", container], check=False)
    run([
        "docker", "run", "-d", "--platform", "linux/amd64", "--name", container,
        "-v", f"{ROOT / 'hh_agent_pro'}:/opt/hh/hh_agent_pro:ro",
        image, "sleep", "infinity",
    ])
    started = time.monotonic()
    record = {"instance_id": iid, "repo": instance["repo"], "difficulty": instance.get("difficulty", "")}
    try:
        commit_baseline(container)
        agent_python = find_agent_python(container)
        if not agent_python:
            record.update(status="infra_error", error="no python>=3.11 in image")
            return record, ""
        prompt = PROMPT.format(repo=instance["repo"], problem=instance["problem_statement"])
        container_env = {
            "PYTHONPATH": "/opt/hh",
            # 让 run_shell 里的 python/pytest 指向仓库的依赖环境。
            "PATH": "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
        cmd = ["docker", "exec", "-i", "-w", "/testbed"]
        for key, value in container_env.items():
            cmd += ["-e", f"{key}={value}"]
        for key in secrets:
            cmd += ["-e", key]  # 只写变量名：值从宿主进程环境传入，不出现在命令行/进程列表里
        cmd += [
            container, agent_python, "-m", "hh_agent_pro",
            "--jsonl", "--cwd", "/testbed", "--provider", "deepseek", "--model", args.model,
            "--approval", "auto", "--toolset", "core",
            "--max-steps", str(args.max_steps),
            "--max-new-tokens", str(args.max_new_tokens),
            "--episode-timeout-ms", str(args.episode_timeout_ms),
        ]
        request = {"protocolVersion": 1, "requestId": f"swe-{iid}", "text": prompt, "sessionId": None}
        proc = subprocess.run(
            cmd, input=json.dumps(request) + "\n", capture_output=True, text=True,
            timeout=args.episode_timeout_ms / 1000 + 300, env={**os.environ, **secrets},
        )
        turn = last_turn(proc.stdout)
        patch = collect_patch(container)
        record.update(
            status=turn.get("status", "agent_error"),
            stop_reason=turn.get("stopReason", ""),
            failure_kind=turn.get("failureKind", ""),
            usage=turn.get("usage", {}),
            patch_lines=patch.count("\n"),
            final_answer=str(turn.get("finalAnswer", ""))[:2000],
            agent_stderr_tail=proc.stderr[-1500:],
        )
        return record, patch
    except subprocess.TimeoutExpired:
        record.update(status="timeout")
        return record, collect_patch(container)
    finally:
        record["latency_s"] = round(time.monotonic() - started, 1)
        # 删容器前把 agent 的 trace/report 拷出来，失败时才能诊断。
        trace_dir = Path(args.out_dir) / "traces" / iid
        trace_dir.mkdir(parents=True, exist_ok=True)
        run(["docker", "cp", f"{container}:/testbed/.hh/runs", str(trace_dir)], check=False)
        if not args.keep_containers:
            run(["docker", "rm", "-f", container], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default="SWE-bench/SWE-bench_Verified")
    parser.add_argument("--instance-ids", nargs="+", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--run-name", required=True, help="Used for model_name_or_path and output names.")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--max-new-tokens", type=int, default=8192)
    parser.add_argument("--episode-timeout-ms", type=int, default=1_800_000)
    parser.add_argument("--keep-containers", action="store_true")
    parser.add_argument(
        "--max-total-tokens", type=int, default=None,
        help="Stop starting new instances once input+output tokens of this run (including earlier resumed ones) exceed this.",
    )
    args = parser.parse_args()

    from datasets import load_dataset

    wanted = set(args.instance_ids)
    instances = [x for x in load_dataset(args.dataset, split="test") if x["instance_id"] in wanted]
    missing = wanted - {x["instance_id"] for x in instances}
    if missing:
        sys.exit(f"unknown instance ids: {sorted(missing)}")

    secrets = load_env_values({"HH_DEEPSEEK_API_KEY", "HH_DEEPSEEK_API_BASE"})
    if not secrets.get("HH_DEEPSEEK_API_KEY"):
        sys.exit("HH_DEEPSEEK_API_KEY missing from .env")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    preds_path = out_dir / f"{args.run_name}.predictions.jsonl"
    meta_path = out_dir / f"{args.run_name}.agent-runs.jsonl"
    done = set()
    if preds_path.exists():
        done = {json.loads(line)["instance_id"] for line in preds_path.read_text().splitlines() if line.strip()}
    used_tokens = 0
    if meta_path.exists():
        for line in meta_path.read_text(encoding="utf-8").splitlines():
            usage = json.loads(line).get("usage") or {}
            used_tokens += int(usage.get("inputTokens", 0) or 0) + int(usage.get("outputTokens", 0) or 0)

    for instance in instances:
        if instance["instance_id"] in done:
            print(f"skip {instance['instance_id']} (already predicted)", flush=True)
            continue
        if args.max_total_tokens and used_tokens >= args.max_total_tokens:
            print(f"stop: token budget reached ({used_tokens} >= {args.max_total_tokens})", flush=True)
            break
        print(f"run  {instance['instance_id']} ...", flush=True)
        record, patch = run_instance(instance, args, secrets)
        with preds_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "instance_id": instance["instance_id"],
                "model_name_or_path": args.run_name,
                "model_patch": patch,
            }) + "\n")
        with meta_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        usage = record.get("usage") or {}
        used_tokens += int(usage.get("inputTokens", 0) or 0) + int(usage.get("outputTokens", 0) or 0)
        print(
            f"done {instance['instance_id']}: {record.get('status')} stop={record.get('stop_reason')} "
            f"requests={record.get('usage', {}).get('requests')} "
            f"input={record.get('usage', {}).get('inputTokens')} "
            f"cached={record.get('usage', {}).get('cacheReadTokens')} "
            f"patch_lines={record.get('patch_lines')} {record.get('latency_s')}s",
            flush=True,
        )


if __name__ == "__main__":
    main()
