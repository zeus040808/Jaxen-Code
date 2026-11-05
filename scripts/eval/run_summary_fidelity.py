"""摘要保真度评测（调用真实模型，会产生少量费用）。

    .venv/bin/python scripts/eval/run_summary_fidelity.py [--min-entries 15] [--model deepseek-flash]

样本：artifacts/swebench/traces 下的完整对话记录，每份在 50% 和 100% 处各截一段。
结果写入 artifacts/evals/summary-fidelity-v1.json（含每段摘要原文，便于人工抽查）。API key 只从 .env 读取，不打印。
"""

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hh_agent_pro.cli import _build_model_client  # noqa: E402
from hh_agent_pro.config import load_project_env  # noqa: E402
from hh_agent_pro.evaluation.summary_fidelity import grade, replay, summary_request, to_history  # noqa: E402


def samples(min_entries):
    for path in sorted((ROOT / "artifacts/swebench/traces").rglob("transcript.jsonl")):
        if sum(1 for _ in path.open(encoding="utf-8")) < min_entries:
            continue
        history = to_history(replay(path))
        instance = next(part for part in path.parts if "__" in part)
        for cut in (0.5, 1.0):
            part = history[: max(2, int(len(history) * cut))]
            yield {"instance": instance, "transcript": str(path.relative_to(ROOT)), "cut": cut, "items": len(part)}, part


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-entries", type=int, default=15)
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default=str(ROOT / "artifacts/evals/summary-fidelity-v1.json"))
    args = parser.parse_args()

    load_project_env(ROOT)  # 只把 .env 读进环境变量，不打印
    client = _build_model_client(SimpleNamespace(provider="deepseek", model=args.model, base_url=None, temperature=0.2,
                                                 request_timeout=300, context_window=None))
    rows, usage = [], {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
    for meta, history in samples(args.min_entries):
        started = time.monotonic()
        summary = str(client.complete(summary_request(history)) or "").strip()
        info = client.last_completion_metadata
        meta.update(grade(history, summary, str(info.get("finish_reason") or "")), latency_s=round(time.monotonic() - started, 1),
                    finish_reason=info.get("finish_reason"), output_tokens=info.get("output_tokens"),
                    reasoning_chars=len(getattr(client, "last_reasoning_content", "") or ""), summary=summary)
        for key in usage:
            usage[key] += int(client.last_completion_metadata.get(key, 0) or 0)
        rows.append(meta)
        recall = meta["recall"]
        print(f"{meta['instance']:32} cut={meta['cut']} items={meta['items']:3} "
              + " ".join(f"{k}={v['kept']}/{v['total']}" for k, v in recall.items())
              + f" invented={len(meta['invented_paths'])} sections={meta['sections_found']}"
              + f" finish={meta['finish_reason']} out={meta['output_tokens']} reasoning={meta['reasoning_chars']}"
              + f" accepted={meta['accepted']} {meta['problem']}", flush=True)

    totals = {}
    for key in rows[0]["recall"] if rows else []:
        kept = sum(row["recall"][key]["kept"] for row in rows)
        total = sum(row["recall"][key]["total"] for row in rows)
        totals[key] = {"kept": kept, "total": total, "recall": round(kept / total, 3) if total else None}
    summary = {"samples": len(rows), "model": client.model, "recall": totals,
               "samples_with_invented_paths": sum(bool(row["invented_paths"]) for row in rows),
               "accepted": sum(row["accepted"] for row in rows),
               "problems": {p: sum(row["problem"] == p for row in rows) for p in {row["problem"] for row in rows} if p},
               "usage": usage}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"schema_version": "summary-fidelity-v1", "summary": summary, "rows": rows},
                                         ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
