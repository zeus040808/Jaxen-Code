"""审查子 agent：主 agent 改完代码准备交付时，派一个只读、上下文全新的子 agent 按清单审查 diff。

为什么要"全新上下文"：主 agent 自己复查时带着当初的推理（"改这一个文件就够了"），很难发现漏洞；
审查者只看到需求和 diff，相当于换一双眼睛。失败题里"在症状处兜底""漏改第二个文件""需求没覆盖全"
都属于这一类。审查清单放在内置 skill（skills/builtin/code-review/SKILL.md）里，是唯一来源。

审查者没有否决权：有问题时把意见交回主 agent，由它决定是否采纳，最多一轮，避免来回拉扯。
同一套审查也给用户自己用：`/review` 审查当前工作区的未提交改动，只出报告、不改代码。
"""

import re
import subprocess
from pathlib import Path, PurePosixPath

from .subagent import run_child
from .progress import CODE_SUFFIXES

CHECKLIST_PATH = Path(__file__).resolve().parent / "skills" / "builtin" / "code-review" / "SKILL.md"
REVIEW_MAX_STEPS = 12
MAX_DIFF_CHARS = 30_000
MAX_NEW_FILE_CHARS = 4_000
REVIEW_MODES = ("off", "auto", "always")


def checklist():
    text = CHECKLIST_PATH.read_text(encoding="utf-8")
    return text.split("---", 2)[-1].strip() if text.startswith("---") else text.strip()


def workspace_diff(root):
    """未提交的改动：git diff HEAD，加上未跟踪的新文件（排除 agent 自己的 .jaxen/）。不是 git 仓库返回空串。"""
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else ""

    parts = [git("diff", "--no-color", "HEAD", "--", ".", ":(exclude).jaxen")]
    for path in git("ls-files", "--others", "--exclude-standard", "--", ".", ":(exclude).jaxen").splitlines():
        # 仓库没写 .gitignore 时，__pycache__ 之类的编译产物也会是"未跟踪文件"；审查它们只会产生噪音
        if "__pycache__" in path or path.endswith((".pyc", ".pyo")):
            continue
        try:
            data = (Path(root) / path).read_bytes()[:MAX_NEW_FILE_CHARS]
        except OSError:
            continue
        if b"\0" in data:  # 二进制文件
            continue
        parts.append(f"new file: {path}\n{data.decode('utf-8', errors='replace')}")
        if len(parts) > 20:
            break
    diff = "\n".join(part for part in parts if part.strip())
    return diff[:MAX_DIFF_CHARS] + ("\n...[diff truncated]" if len(diff) > MAX_DIFF_CHARS else "")


def changed_paths(diff):
    """从 workspace_diff 的输出里取出改动的文件路径。"""
    paths = re.findall(r"^diff --git a/(\S+)", diff, re.MULTILINE)
    paths += re.findall(r"^new file: (\S+)", diff, re.MULTILINE)
    return list(dict.fromkeys(paths))


def is_test_path(path):
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return any(part in {"tests", "test", "testing"} for part in parts[:-1]) or name.startswith("test_") or \
        name.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js"))


def needs_review(mode, diff):
    """三档：off 不审；always 有改动就审；auto 只有改了非测试的源代码才审（只改文档、配置、测试时跳过）。

    不按改动行数判断：xarray-6938 只改了 1 行，恰恰是"在症状处兜底"、最该审的那种。
    """
    if mode == "off" or not diff.strip():
        return False
    if mode == "always":
        return True
    return any(path.lower().endswith(tuple(CODE_SUFFIXES)) and not is_test_path(path) for path in changed_paths(diff))


def build_review_task(request, diff):
    return (
        "You are an independent code reviewer. Another agent made the change below; you cannot edit files.\n\n"
        f"## Request the change must satisfy\n{request.strip()}\n\n"
        f"## Change (diff)\n```diff\n{diff}\n```\n\n"
        f"## Checklist\n{checklist()}\n\n"
        "Use read_file, search, and repo_map to check the changed code and its callers. "
        "End your final answer with exactly one line `VERDICT: approve` or `VERDICT: issues`, "
        "followed (for issues) by one bullet per problem: `- path: problem; what to change`."
    )


def parse_review(answer):
    """取出结论和问题列表。格式不对时按"通过"处理：审查是辅助，不能因为它的输出格式卡住交付。"""
    match = re.search(r"VERDICT:\s*(approve|issues)", answer or "", re.IGNORECASE)
    verdict = match.group(1).lower() if match else "approve"
    if verdict == "approve":
        return {"verdict": verdict, "issues": [], "parsed": bool(match)}
    # 子 agent 自己有未解决的工具失败时，系统会在它的回答末尾追加 "Unresolved at delivery:" 清单，那不是审查意见
    tail = (answer or "")[match.end():].split("Unresolved at delivery")[0]
    issues = [line.strip()[1:].strip() for line in tail.splitlines() if line.strip().startswith(("-", "*"))]
    if verdict == "issues" and not issues:
        issues = [tail.strip() or "the reviewer reported issues without details"]
    return {"verdict": verdict, "issues": issues[:10], "parsed": bool(match)}


def run_review(agent, request, diff):
    answer, report = run_child(agent, "review", build_review_task(request, diff), REVIEW_MAX_STEPS)
    return {**parse_review(answer), "status": report.status, "answer": answer, "usage": report.usage}


def feedback_notice(review):
    """有问题时交回主 agent 的话：只此一轮，由主 agent 判断采纳与否。"""
    return (
        "Independent code review (one round) of your change found possible problems:\n"
        + "\n".join(f"- {issue}" for issue in review["issues"])
        + "\nCheck each one against the code. Fix the valid ones with tools, then finish again; "
        "if an item is wrong, say why in your final answer. There will be no second review."
    )


def review_report(review):
    """`/review` 给用户看的报告。"""
    if review["verdict"] == "approve":
        return "## Code review\n\nVerdict: **approve**: no problems that make the change wrong or incomplete.\n\n" + review["answer"]
    return "## Code review\n\nVerdict: **issues**\n\n" + "\n".join(f"- {issue}" for issue in review["issues"])
