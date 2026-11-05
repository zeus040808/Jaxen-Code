"""评测流水线：模型接口不可用（model_error）时自动等待重跑。

问题：2026-11-03 中等 50 道有 2 道因模型接口故障失败，手工补跑了 4 次，还踩了"只删 results 不删 predictions，
预测脚本跳过、流水线拿旧记录再判一次分"的坑。改法：run_pipeline 碰到 model_error 自动删掉这道题的预测、等待后重跑。
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "swebench"))
import run_pipeline  # noqa: E402


def fake_predictions_script(tmp_path, stop_reasons):
    """替身：像 run_hh_predictions.py 一样，已有预测就跳过；否则追加一行预测和一行运行记录。"""
    preds = tmp_path / "run.predictions.jsonl"
    meta = tmp_path / "run.agent-runs.jsonl"
    calls = []

    def fake_run(command, capture_output, text):
        done = {json.loads(line)["instance_id"] for line in preds.read_text().splitlines()} if preds.exists() else set()
        if "iid" in done:
            calls.append("skipped")
            return SimpleNamespace(stdout="skip iid (already predicted)\n")
        stop = stop_reasons[len([c for c in calls if c != "skipped"])]
        calls.append(stop)
        with preds.open("a") as handle:
            handle.write(json.dumps({"instance_id": "iid", "model_patch": ""}) + "\n")
        with meta.open("a") as handle:
            handle.write(json.dumps({"instance_id": "iid", "stop_reason": stop, "usage": {"inputTokens": 10}}) + "\n")
        return SimpleNamespace(stdout="done\n")

    return fake_run, calls, preds, meta


def make_args(tmp_path, retries=2):
    return SimpleNamespace(out_dir=str(tmp_path), run_name="run", infra_retries=retries, infra_wait=0)


def test_model_error_is_retried_until_the_agent_really_runs(tmp_path, monkeypatch):
    fake_run, calls, preds, meta = fake_predictions_script(tmp_path, ["model_error", "final_answer_returned"])
    monkeypatch.setattr(run_pipeline.subprocess, "run", fake_run)
    monkeypatch.setattr(run_pipeline.time, "sleep", lambda seconds: None)

    _, record, retries = run_pipeline.run_agent(make_args(tmp_path), "iid", ["cmd"])

    assert calls == ["model_error", "final_answer_returned"]  # 第二次真的重跑了，没有被"已有预测"跳过
    assert record["stop_reason"] == "final_answer_returned" and retries == 1
    assert len(preds.read_text().splitlines()) == 1  # 只剩重跑后的那份预测
    assert len(meta.read_text().splitlines()) == 2  # 运行记录保留：失败那次的 token 仍计入预算


def test_retries_are_bounded(tmp_path, monkeypatch):
    fake_run, calls, _, _ = fake_predictions_script(tmp_path, ["model_error"] * 5)
    monkeypatch.setattr(run_pipeline.subprocess, "run", fake_run)
    monkeypatch.setattr(run_pipeline.time, "sleep", lambda seconds: None)

    _, record, retries = run_pipeline.run_agent(make_args(tmp_path, retries=2), "iid", ["cmd"])

    assert calls == ["model_error"] * 3 and retries == 2 and record["stop_reason"] == "model_error"


def test_solving_failures_are_not_retried(tmp_path, monkeypatch):
    # 题目本身没做对（正常结束、被拦截等）不是基础设施问题，不重跑
    fake_run, calls, _, _ = fake_predictions_script(tmp_path, ["incomplete_delivery", "final_answer_returned"])
    monkeypatch.setattr(run_pipeline.subprocess, "run", fake_run)

    _, record, retries = run_pipeline.run_agent(make_args(tmp_path), "iid", ["cmd"])

    assert calls == ["incomplete_delivery"] and retries == 0
