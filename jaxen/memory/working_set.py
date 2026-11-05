"""当前任务的极小工作状态。

这里不是长期记忆，也不是历史摘要。它只回答两个问题：
1. 这一轮正在做什么？
2. 最近碰过哪些文件？

完整对话由 Session 保存；文件内容证据由 SourceEvidence 保存；稳定事实由
LongTermMemory 保存。把边界写死，避免同一信息出现四份副本。
"""

from pathlib import Path

from ..workspace import clip

SCHEMA_VERSION = 3
RECENT_FILE_LIMIT = 8


def default_working_set_state():
    return {
        "schema_version": SCHEMA_VERSION,
        "task_summary": "",
        "recent_files": [],
    }


def _canonical_path(raw_path, workspace_root=None):
    path = Path(str(raw_path))
    if workspace_root is None:
        return path.as_posix()
    root = Path(workspace_root).resolve()
    resolved = (path if path.is_absolute() else root / path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def normalize_working_set_state(state, workspace_root=None):
    """读取新结构，并兼容旧 LayeredMemory Session。

    旧结构中的 file_summaries、episodic_notes、process_notes 故意不迁移：
    它们分别已由 evidence、session archive 和 task/tool state 接管。
    """

    state = state if isinstance(state, dict) else {}
    legacy_working = state.get("working") if isinstance(state.get("working"), dict) else {}
    task_summary = state.get("task_summary", legacy_working.get("task_summary", state.get("task", "")))
    recent_files = state.get("recent_files", legacy_working.get("recent_files", state.get("files", [])))
    if not isinstance(recent_files, (list, tuple, set)):
        recent_files = [recent_files] if recent_files else []

    normalized_files = []
    for raw_path in recent_files:
        if not str(raw_path).strip():
            continue
        path = _canonical_path(raw_path, workspace_root)
        if path in normalized_files:
            normalized_files.remove(path)
        normalized_files.append(path)

    return {
        "schema_version": SCHEMA_VERSION,
        "task_summary": clip(str(task_summary).strip(), 300),
        "recent_files": normalized_files[-RECENT_FILE_LIMIT:],
    }


class WorkingSet:
    """A tiny current-task dashboard, despite the historical ``memory`` name."""

    def __init__(self, state=None, workspace_root=None):
        self.workspace_root = workspace_root
        self.state = normalize_working_set_state(state, workspace_root)

    def to_dict(self):
        self.state = normalize_working_set_state(self.state, self.workspace_root)
        return self.state

    def canonical_path(self, path):
        return _canonical_path(path, self.workspace_root)

    def set_task_summary(self, summary):
        self.state["task_summary"] = clip(str(summary).strip(), 300)
        return self

    def remember_file(self, path):
        path = self.canonical_path(path)
        files = [item for item in self.state["recent_files"] if item != path]
        files.append(path)
        self.state["recent_files"] = files[-RECENT_FILE_LIMIT:]
        return self

    def render(self):
        return "\n".join(
            [
                "Current working state:",
                "- lifetime: current session/task only; never treated as a durable fact",
                f"- task: {self.state['task_summary'] or '-'}",
                f"- recent_files: {', '.join(self.state['recent_files']) or '-'}",
            ]
        )

    # Compatibility with callers while the public runtime attribute remains
    # ``agent.working_set``. New code should call render().
