"""按 SWE-rebench 题目自带的 install_config 在本地构建任务镜像（Docker Hub 拉不下来时用）。

每题生成一个 Dockerfile：官方 Python 基础镜像（经 DaoCloud 镜像源）→ 从 GitHub 检出 base_commit 到 /testbed
→ 执行题目给的 pre_install / install 命令（pip 只装题目创建日期之前发布的版本）。构建结果打上数据集里的镜像名（docker_image），
跑 agent 和判分的脚本不用改。环境是否搭对，由 rebench_grade.py --gold（官方补丁必须通过）把关。

    .venv-swebench/bin/python -I scripts/swebench/rebench_build.py --split 2026_03 --instance-ids <ids...>
"""

import argparse
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

DATASET = "nebius/SWE-rebench-leaderboard"
BASE_IMAGE = "docker.m.daocloud.io/library/python:{python}"  # 完整版自带 git 和编译工具


def dockerfile(instance):
    config = instance["install_config"]
    steps = [f"FROM {BASE_IMAGE.format(python=config['python'])}",
             "ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore",
             # 依赖只装题目创建（PR 提交）之前发布的版本，还原当时的环境。否则今天的新版本可能多出
             # 弃用警告或行为变化，让官方补丁也过不了（pypa/build-1027：新 setuptools 的警告被测试当成错误）。
             f"ENV PIP_UPLOADED_PRIOR_TO={instance['created_at'].replace(' ', 'T')}Z",
             # 完整克隆而不是浅克隆：setuptools_scm 一类工具要靠 git 历史和 tag 算版本号。
             f"RUN git clone --quiet https://github.com/{instance['repo']}.git /testbed "
             f"&& git -C /testbed checkout --quiet {instance['base_commit']}",
             "WORKDIR /testbed"]
    for command in list(config.get("pre_install") or []):
        steps.append(f"RUN {command}")
    if config.get("pip_packages") is not None and len(config["pip_packages"]):
        steps.append("RUN pip install " + " ".join(shlex.quote(p) for p in config["pip_packages"]))
    for path in list(config.get("reqs_path") or []):
        steps.append(f"RUN pip install -r {shlex.quote(path)}")
    if config.get("install"):
        steps.append(f"RUN {config['install']}")
    return "\n".join(steps) + "\n"


def build(instance, log_dir):
    iid = instance["instance_id"]
    with tempfile.TemporaryDirectory() as context:
        (Path(context) / "Dockerfile").write_text(dockerfile(instance), encoding="utf-8")
        started = time.monotonic()
        result = subprocess.run(
            ["docker", "build", "--platform", "linux/amd64", "--progress", "plain",
             "-t", instance["docker_image"], context],
            capture_output=True, text=True,
        )
    (log_dir / f"{iid}.build.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    seconds = round(time.monotonic() - started)
    return result.returncode == 0, seconds


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--split", required=True)
    parser.add_argument("--instance-ids", nargs="+", required=True)
    parser.add_argument("--log-dir", default="artifacts/swebench/rebench-build")
    args = parser.parse_args()

    from datasets import load_dataset

    instances = {x["instance_id"]: x for x in load_dataset(DATASET, split=args.split)}
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    for iid in args.instance_ids:
        ok, seconds = build(instances[iid], log_dir)
        print(f"{iid}: {'built' if ok else 'BUILD FAILED'} in {seconds}s (log: {log_dir / (iid + '.build.log')})", flush=True)


if __name__ == "__main__":
    main()
