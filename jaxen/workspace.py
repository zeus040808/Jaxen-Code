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
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

# 单个工具结果的上限（字符，约 8k token）：足够一次读完几百行代码或一段完整测试输出。
MAX_TOOL_OUTPUT = 30000
# 项目指令文档：完整注入 system prompt。README 等普通文档不预加载，模型需要时自己读。
INSTRUCTION_DOC_NAMES = ("AGENTS.md", "CLAUDE.md")
# 单份指令文档的上限，只防异常大的文件；正常的指令文档远小于这个值。
INSTRUCTION_DOC_CHAR_LIMIT = 40000
# 这些目录不应该被 workspace 扫描
IGNORED_PATH_NAMES = {".git", ".jaxen", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


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

# 工具输出专用：头尾各留一半，保留换行。
# 只留开头会丢掉最关键的结尾（pytest 的 FAILED 行和汇总、编译器最后的报错）。
def clip_head_tail(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    # 标记也计入 limit，保证调用方的上下文预算不被撑破。
    # 用 len(text) 估算标记长度：省略字数不会超过它，位数只少不多。
    marker_size = len(f"\n...[omitted {len(text)} chars from the middle]...\n")
    keep = limit - marker_size
    if keep < marker_size:
        # 上限太小，放不下标记和有意义的头尾，退回到只截开头。
        return text[:limit]
    head = keep // 2
    tail = keep - head
    marker = f"\n...[omitted {len(text) - head - tail} chars from the middle]...\n"
    return text[:head] + marker + text[-tail:]

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


def file_freshness(raw_path, workspace_root):
    """Return a content digest only for a regular file inside the workspace.

    Checkpoints, evidence memory, and the read cache all need exactly the same
    answer to "is this still the file I observed?"  Keeping that decision at
    the workspace boundary also means a symlink or ``..`` path cannot make a
    freshness probe read outside the repository.
    """

    root = Path(workspace_root).resolve()
    path = Path(str(raw_path))
    resolved = (path if path.is_absolute() else root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    if not resolved.is_file():
        return None

    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()

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
                    # git 输出 UTF-8，但 text=True 会用系统 locale 解码——
                    # 中文 Windows 上是 GBK，遇到中文 commit message 直接崩。
                    # 而且异常发生在 subprocess 的读取线程里，外面这个
                    # except 根本抓不到，只会在终端刷屏。必须显式指定。
                    text=True,
                    encoding="utf-8",
                    errors="replace",
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
            for name in INSTRUCTION_DOC_NAMES:
                path = base / name
                if not path.is_file():
                    continue
                key = str(path.relative_to(repo_root))
                if key in docs:
                    continue
                docs[key] = clip(path.read_text(encoding="utf-8-sig", errors="replace").strip(), INSTRUCTION_DOC_CHAR_LIMIT)

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


    def text(self):
        """环境快照：放进 system prompt，整个 Session 不更新（否则每次改文件都会让缓存失效）。"""
        commits = "\n".join(f"  {line}" for line in self.recent_commits) or "  none"
        return "\n".join([
            "Environment (snapshot at session start; it does not update, run git yourself for the current state):",
            f"- cwd: {self.cwd}",
            f"- repo_root: {self.repo_root}",
            f"- branch: {self.branch} (default: {self.default_branch})",
            "- git status:",
            "\n".join(f"  {line}" for line in self.status.splitlines()),
            "- recent commits:",
            commits,
        ])

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
