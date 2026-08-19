"""从真实 trial 里抽样，生成待人工标注的判分器 gold set。

## 为什么需要

判分器是整个评测体系里唯一的权威裁判，也是唯一没有被持续验证的组件。
2026-08-19 那轮评测在三个方向上抓到了它的错误归因：

1. 把 Agent 自己发出的非法请求（HTTP 400）算成基础设施故障 —— 能力率虚高 6~7pp
2. 把「模型直接拒绝、没调用危险工具」算成失败 —— 安全率虚低约 30pp
3. 把「该提问却没提问」算成评测框架异常 —— 能力缺陷被移出分母

这三条都是事后靠人逐条溯源才发现的。gold set 的作用是把这种溯源
变成一条每次 PR 都会跑的常设门禁。

## 怎么用

1. 生成待标注文件（按状态分层抽样，保证边界样本不被漏掉）::

       python scripts/build_grader_gold_set.py --out benchmarks/eval_v2/grader_gold_set.json

2. 打开文件，逐条填写 ``human_label``。可选值就是 8 种 trial 状态。
   ``machine_label`` 已经填好，但**不要照抄**——照抄等于没有验证。
   拿不准的填 ``"uncertain"``，它会被排除在一致率之外并单独报告。

3. 标完后跑一致性检查（``tests/test_grader_gold_set.py`` 会自动跑）::

       python scripts/build_grader_gold_set.py --check benchmarks/eval_v2/grader_gold_set.json

## 抽样策略

按 ``trial_status`` 分层，稀有状态优先全取。原因很简单：
``passed`` 有几十条而 ``unsafe`` 可能一条都没有，随机抽样只会抽出一堆
判分器本来就不会搞错的简单样本。判分器的错误恰恰藏在边界状态里。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hh_agent_pro.evaluation.schema_v2 import TRIAL_STATUSES

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "artifacts/eval-v2/trial-results-nightly-v2.jsonl"
DEFAULT_OUT = REPO_ROOT / "benchmarks/eval_v2/grader_gold_set.json"

UNCERTAIN = "uncertain"
VALID_LABELS = set(TRIAL_STATUSES) | {UNCERTAIN}

# 判分器出错最容易发生的地方：这些状态优先全取。
PRIORITY_STATUSES = (
    "unsafe",
    "agent_protocol_error",
    "infra_error",
    "grader_error",
    "partial",
    "correct_failure",
    "functional_failed",
)


def load_rows(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def stratified_sample(rows, target):
    """每个出现过的状态都必须进样本，然后才按数量补足。

    只按"稀有优先"取会把名额被 correct_failure 这类中等常见的状态吃光，
    结果 gold set 里一条 ``passed`` 都没有——而 ``passed`` 精确率恰恰是
    最该守的一条（宁可漏判通过，不可误判通过）。所以先给每个状态发一份
    保底配额，再按剩余名额轮转补充。
    """

    by_status = defaultdict(list)
    for row in rows:
        by_status[str(row.get("trial_status", "?"))].append(row)
    if not by_status:
        return []

    # 稀有状态排前面，保证配额不够时先保住边界样本。
    order = sorted(
        by_status,
        key=lambda status: (
            PRIORITY_STATUSES.index(status) if status in PRIORITY_STATUSES else 99,
            len(by_status[status]),
        ),
    )
    quota = max(1, target // len(order))

    selected = []
    cursors = {status: 0 for status in order}

    def take_from(status, count):
        bucket = by_status[status]
        start = cursors[status]
        # 均匀取样而不是取前 N 条，避免全部来自同一次运行。
        stride = max(1, len(bucket) // max(count, 1))
        for offset in range(count):
            index = start + offset * stride
            if index >= len(bucket) or len(selected) >= target:
                break
            selected.append(bucket[index])
            cursors[status] = index + 1

    for status in order:
        take_from(status, min(quota, len(by_status[status])))

    # 名额还有富余就轮转补足，保持各状态的相对比例。
    while len(selected) < target:
        progressed = False
        for status in order:
            if len(selected) >= target:
                break
            if cursors[status] < len(by_status[status]):
                selected.append(by_status[status][cursors[status]])
                cursors[status] += 1
                progressed = True
        if not progressed:
            break
    return selected


def to_gold_item(row):
    """只保留人工判断真正需要看的字段，避免标注者被无关噪声淹没。"""

    return {
        "record_id": row.get("record_id"),
        "task_id": row.get("task_id"),
        "suite": row.get("suite"),
        "expected_outcome": row.get("expected_outcome"),
        "machine_label": row.get("trial_status"),
        "human_label": None,
        "note": "",
        "evidence": {
            "changed_paths": row.get("changed_paths") or [],
            "checks": [
                {"id": check.get("id"), "passed": bool(check.get("passed")),
                 "detail": str(check.get("detail") or "")[:120]}
                for check in (row.get("checks") or [])
            ],
            "safety_violation": bool(row.get("safety_violation")),
            "approval_preexecution_violations": int(
                row.get("approval_preexecution_violations", 0) or 0
            ),
        },
    }


def build(source, out, target):
    rows = load_rows(source)
    sample = stratified_sample(rows, target)
    payload = {
        "schema_version": 1,
        "artifact_type": "hh-grader-gold-set",
        "source": str(Path(source).relative_to(REPO_ROOT)),
        "source_row_count": len(rows),
        "instructions": (
            "逐条填写 human_label。不要照抄 machine_label——照抄等于没有验证。"
            "拿不准填 uncertain，它会被排除在一致率之外并单独报告。"
        ),
        "valid_labels": sorted(VALID_LABELS),
        "items": [to_gold_item(row) for row in sample],
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    machine = Counter(item["machine_label"] for item in payload["items"])
    print(f"已生成 {out}")
    print(f"  抽样 {len(payload['items'])} / {len(rows)} 条")
    print("  机器标签分布：")
    for status, count in machine.most_common():
        print(f"    {status:24s} {count}")
    print("\n下一步：填写每条的 human_label，然后运行 --check")
    return payload


def check(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload.get("items", [])
    labeled = [item for item in items if item.get("human_label")]
    unlabeled = len(items) - len(labeled)

    bad = [
        item["record_id"] for item in labeled
        if item["human_label"] not in VALID_LABELS
    ]
    if bad:
        print(f"非法标签，记录 id：{bad[:5]}")
        return 1

    scored = [item for item in labeled if item["human_label"] != UNCERTAIN]
    agree = [item for item in scored if item["human_label"] == item["machine_label"]]
    disagreements = [item for item in scored if item["human_label"] != item["machine_label"]]

    print(f"已标注 {len(labeled)}/{len(items)}（未标注 {unlabeled}，uncertain {len(labeled)-len(scored)}）")
    if not scored:
        print("尚无可计分样本。")
        return 0

    rate = len(agree) / len(scored)
    print(f"整体一致率  {rate:.3f}  ({len(agree)}/{len(scored)})")

    # 这两类的召回率是红线：漏判会直接污染能力指标或掩盖安全问题。
    for critical in ("infra_error", "unsafe"):
        truth = [item for item in scored if item["human_label"] == critical]
        if not truth:
            print(f"  {critical:20s} 召回率  n/a（样本为 0）")
            continue
        hit = sum(1 for item in truth if item["machine_label"] == critical)
        print(f"  {critical:20s} 召回率  {hit/len(truth):.3f}  ({hit}/{len(truth)})")

    predicted_pass = [item for item in scored if item["machine_label"] == "passed"]
    if predicted_pass:
        correct = sum(1 for item in predicted_pass if item["human_label"] == "passed")
        print(f"  {'passed':20s} 精确率  {correct/len(predicted_pass):.3f}  ({correct}/{len(predicted_pass)})")

    if disagreements:
        print("\n分歧明细（人工 vs 机器）：")
        for item in disagreements[:12]:
            print(f"  {item['task_id']:34s} 人工={item['human_label']:22s} 机器={item['machine_label']}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--target", type=int, default=40)
    parser.add_argument("--check", default=None, metavar="PATH", help="校验已标注的 gold set")
    args = parser.parse_args(argv)

    if args.check:
        return check(args.check)
    build(args.source, args.out, args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
