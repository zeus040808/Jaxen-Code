"""摘要保真度：真实模型用现有摘要提示词压缩真实对话，程序检查关键信息有没有保住、有没有编造。

样本：SWE-bench 的完整对话记录（transcript.jsonl）逐轮还原成历史，在 50% 和 100% 处各截一段。
摘要请求和 ContextManager._summarize() 完全一样（同一提示词、每条最多 4000 字符、最多输出 4096 token）。

关键信息（从历史里用程序抽取，作为标准答案）：
    changed_files   patch_file / write_file 改过的文件            —— 丢了就不知道改过什么
    test_targets    跑过的测试命令里的测试文件 / 模块
    errors          失败输出里的异常类型（如 AssertionError）
    read_files      读过的文件（参考项：读过的文件很多，不要求全记）
编造：摘要里提到、但历史里从没出现过的 .py 路径。
"""

import json
import re
from pathlib import Path

from ..context_manager import build_summary_request, summary_problem

REMINDER = re.compile(r"\n*<system-reminder>.*?</system-reminder>", re.S)
PY_PATH = re.compile(r"[\w./-]*\w\.py\b")
EXCEPTION = re.compile(r"\b([A-Z]\w*(?:Error|Exception|Failure))\b")
TEST_FILE = re.compile(r"[\w./-]*test[\w-]*\.py")
TEST_MODULE = re.compile(r"\b\w+(?:\.\w+)*\.tests?(?:\.\w+)*\b(?!\()|\b\w+(?:\.\w+)*\.test_\w+\b(?!\()")


def _norm(path):
    return str(path).removeprefix("/testbed/").removeprefix("./")


def replay(transcript_path):
    """transcript.jsonl → 最后一轮发给模型的完整消息列表（kept_messages 条沿用上一轮 + new_messages）。"""
    messages = []
    for line in Path(transcript_path).read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("kind") == "request":
            messages = messages[: entry["kept_messages"]] + entry["new_messages"]
    return messages


def to_history(messages):
    """Chat 消息 → session 风格的条目（去掉每轮的 system-reminder：session 历史里本来就没有它）。"""
    names, items = {}, []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                names[call.get("id")] = function.get("name", "")
                calls.append({"id": call.get("id"), "name": function.get("name", ""), "args": args})
            items.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            items.append({"role": "tool", "name": names.get(message.get("tool_call_id"), ""), "content": message.get("content") or ""})
        elif role in {"user", "assistant"}:
            content = REMINDER.sub("", str(message.get("content") or "")).strip()
            if content:
                items.append({"role": role, "content": content})
    return items


def summary_request(history):
    """和 ContextManager._summarize() 用同一个函数组装，保证测的就是线上发出去的请求。"""
    return build_summary_request(history)


def _test_targets(command):
    """测试文件路径（…test….py）和点号模块（migrations.test_autodetector）；/testbed 这类目录不算。"""
    return {_norm(t) for t in TEST_FILE.findall(command)} | set(TEST_MODULE.findall(command))


def key_facts(history):
    facts = {"changed_files": set(), "test_targets": set(), "errors": set(), "read_files": set()}
    for item in history:
        for call in item.get("tool_calls") or []:
            name, args = call["name"], call.get("args") or {}
            if name in {"patch_file", "write_file"} and args.get("path"):
                facts["changed_files"].add(_norm(args["path"]))
            elif name in {"read_file"} and args.get("path"):
                facts["read_files"].add(_norm(args["path"]))
            elif name == "run_shell":
                facts["test_targets"].update(_test_targets(str(args.get("command", ""))))
        if item.get("role") == "tool" and item.get("name") == "run_shell" and "exit_code: 0" not in str(item.get("content", ""))[:40]:
            facts["errors"].update(EXCEPTION.findall(str(item.get("content", ""))))
    return {key: sorted(value) for key, value in facts.items()}


def _mentioned(fact, summary):
    """路径允许只写文件名或去掉前缀；其余按原文（不区分大小写）找。"""
    lowered = summary.lower()
    if fact.lower() in lowered:
        return True
    if fact.endswith(".py"):
        return Path(fact).name.lower() in lowered
    return False


def grade(history, summary, finish_reason=""):
    facts = key_facts(history)
    history_text = json.dumps(history, ensure_ascii=False)
    recall = {}
    for key, values in facts.items():
        hits = [value for value in values if _mentioned(value, summary)]
        recall[key] = {"total": len(values), "kept": len(hits), "missing": [v for v in values if v not in hits][:10]}
    invented = sorted({path for path in PY_PATH.findall(summary)
                       if path not in history_text and Path(path).name not in history_text})
    sections = sum(f"{n}." in summary for n in range(1, 6))
    problem = summary_problem(summary, finish_reason)  # 和线上同一个校验：不合格的会被拒绝、退回轻压缩
    return {"recall": recall, "invented_paths": invented, "sections_found": sections, "summary_chars": len(summary),
            "accepted": not problem, "problem": problem}
