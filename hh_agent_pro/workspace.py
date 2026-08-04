"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的“仓库第一印象”。
这份快照刻意保持小而稳定：主要包含 Git 事实和少量白名单项目文档。
给 Agent 构建当前代码仓库的工作区上下文，并提供一些安全、简洁的文本工具

workspace必须知道
当前工作目录在哪里
当前 Git 分支是什么
项目里有哪些重要文档
最近有哪些提交
哪些目录不应该扫描
路径怎么截断显示
文本太长怎么裁剪
"""

import subprocess
import textwrap
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

MAX_TOOL_OUTPUT = 12000
MAX_HISTORY = 12000
# 这些文件最可能直接影响 agent 的行动方式。
# 我们不会预加载整个仓库，只会先给模型一小份“导航包”。
# 这是 workspace 快照会主动预读的文件白名单
DOC_NAMES = ("CLAUDE.md","AGENTS.md", "README.md", "pyproject.toml", "package.json")
# 这些目录不应该被 workspace 扫描
IGNORED_PATH_NAMES = {".git", ".hh", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


def now():
    return datetime.now(timezone.utc).isoformat()

# 1. 把输入转成字符串
# 2. 如果没超长，原样返回
# 3. 如果超长，只保留前 limit 个字符，并说明截断了多少字符
def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"

# 保留开头和结尾
def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]

# 一次构建出来的 workspace 快照
class WorkspaceContext:
    def __init__(self, cwd, repo_root, branch, default_branch, status, recent_commits, project_docs):
        self.cwd = cwd
        # git 仓库根目录
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        # 最近 5 条提交
        self.recent_commits = recent_commits
        # 预读的重要项目文档
        self.project_docs = project_docs

    @classmethod
    # cls是当前类
    def build(cls, cwd, repo_root_override=None):
        # 把路径转成绝对路径
        cwd = Path(cwd).resolve()

        # 内部函数专门执行 git 命令
        def git(args, fallback=""):
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,  # 捕获 stdout/stderr
                    text=True,            # 输出按字符串处理
                    check=True,           # git 命令失败就抛异常
                    timeout=5,
                )
                return result.stdout.strip() or fallback
            except Exception:
                return fallback

        # 获取根目录
        repo_root = (
            Path(repo_root_override).resolve()
            # 外部指定了 repo_root_override
            if repo_root_override is not None
            # 正常运行时通过 git 获取根目录
            else Path(git(["rev-parse", "--show-toplevel"], str(cwd))).resolve()
        )

        # 收集项目文档 docs
        docs = {}
        # 同时扫描 repo_root 和 cwd，这样在子目录启动时也能看到本地文档；
        # 但用相对路径做 key，避免同一份文档被重复收集。
        for base in (repo_root, cwd):
            for name in DOC_NAMES:
                path = base / name
                if not path.exists():
                    continue
                key = str(path.relative_to(repo_root))
                if key in docs:
                    continue
                docs[key] = clip(path.read_text(encoding="utf-8", errors="replace"), 1200)

        # 返回值是项目背景说明
        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            # 拿当前分支名
            branch=git(["branch", "--show-current"], "-") or "-",
            default_branch=(
                lambda branch: branch[len("origin/") :] if branch.startswith("origin/") else branch
            )(git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main") or "origin/main"),
            status=clip(git(["status", "--short"], "clean") or "clean", 1500),
            recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],
            project_docs=docs,
        )


    # workspace 对象转成模型能看的文本
    def text(self):
        # 这段文本会被塞进 prompt prefix，作为相对稳定的基线上下文。
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        return textwrap.dedent(
            f"""\
            Workspace:
            - cwd: {self.cwd}
            - repo_root: {self.repo_root}
            - branch: {self.branch}
            - default_branch: {self.default_branch}
            - status:
            {self.status}
            - recent_commits:
            {commits}
            - project_docs:
            {docs}
            """
        ).strip()

    # 把当前 workspace 快照变成一个 hash
    def fingerprint(self):
        # 这个指纹用来判断仓库状态是否发生了足够大的变化，
        # 从而决定是否需要重建缓存中的 prompt prefix。
        payload = {
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "status": self.status,
            "recent_commits": list(self.recent_commits),
            "project_docs": dict(self.project_docs),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
