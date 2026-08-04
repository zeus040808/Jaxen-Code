"""S12 task system: file-persisted tasks with dependency checks."""
import json
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# 定义任务目录
TASKS_DIR = Path.cwd() / ".tasks"

# 任务状态
VALID_STATUSES = {"pending", "in_progress", "completed"}


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None   #哪个 Agent 正在执行
    blockedBy: list[str]   #必须先完成的任务 ID

# 把任务 ID 转成文件路径
def _task_path(task_id: str) -> Path:
    """Return a task path while preventing IDs from escaping .tasks/."""
    if not task_id.startswith("task_") or not task_id.replace("_", "").isalnum():
        raise ValueError(f"非法任务 ID: {task_id}")
    return TASKS_DIR / f"{task_id}.json"


def save_task(task: Task) -> None:
    if task.status not in VALID_STATUSES:
        raise ValueError(f"非法任务状态: {task.status}")
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    _task_path(task.id).write_text(
        # asdict 将 Task 转成字典。
        json.dumps(asdict(task), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# 加载任务
def load_task(task_id: str) -> Task:
    data = json.loads(_task_path(task_id).read_text(encoding="utf-8"))
    task = Task(**data)
    if task.status not in VALID_STATUSES:
        raise ValueError(f"非法任务状态: {task.status}")
    return task

# 创建任务  blockedBy 存的是任务 ID，而不是标题，因为标题可能重复。
def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    # strip() 会删除首尾空格，所以这些标题都会被拒绝
    if not subject.strip():
        raise ValueError("任务标题不能为空")
    # 仅校验 ID 格式，不要求依赖文件当前一定存在
    dependencies = blockedBy or []
    for task_id in dependencies:
        _task_path(task_id)
    task = Task(
        id=f"task_{int(time.time())}_{secrets.token_hex(2)}",
        subject=subject.strip(),
        description=description,
        status="pending",
        owner=None,
        blockedBy=dependencies,
    )
    save_task(task)
    return task


def list_tasks() -> list[Task]:
    if not TASKS_DIR.exists():
        return []
    return [load_task(path.stem) for path in sorted(TASKS_DIR.glob("task_*.json"))]

# 依赖检查（可以开始 = 所有 blockedBy 任务都存在，并且全部 completed）
def can_start(task_id: str) -> bool:
    task = load_task(task_id)
    for dependency_id in task.blockedBy:
        if not _task_path(dependency_id).exists():
            return False
        if load_task(dependency_id).status != "completed":
            return False
    return True

# 认领任务
def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"任务 {task_id} 当前为 {task.status}，无法认领"
    # 如果有阻塞
    if not can_start(task_id):
        blocked = [dep for dep in task.blockedBy
                   if not _task_path(dep).exists() or load_task(dep).status != "completed"]
        return f"仍被这些任务阻塞: {', '.join(blocked)}"
    # 记录认领者
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    return f"已认领 {task.id}: {task.subject}"

# 完成任务
def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"任务 {task_id} 当前为 {task.status}，无法完成"
    task.status = "completed"
    save_task(task)
    # 扫描其他任务
    unblocked = [item.subject for item in list_tasks()
                 # 只关心还没开始的任务、只关心存在依赖的任务、检查它现在的所有依赖是不是都完成了
                 if item.status == "pending" and item.blockedBy and can_start(item.id)]
    result = f"已完成 {task.id}: {task.subject}"
    if unblocked:
        result += f"\n已解锁: {', '.join(unblocked)}"
    return result


def get_task(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), ensure_ascii=False, indent=2)

# 把任务对象转换成列表文本
def format_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "暂无持久化任务"
    icons = {"pending": " ", "in_progress": ">", "completed": "x"}
    lines = []
    for task in tasks:
        owner = f" owner={task.owner}" if task.owner else ""
        deps = f" blockedBy={','.join(task.blockedBy)}" if task.blockedBy else ""
        lines.append(f"[{icons[task.status]}] {task.id}: {task.subject} [{task.status}]{owner}{deps}")
    return "\n".join(lines)
