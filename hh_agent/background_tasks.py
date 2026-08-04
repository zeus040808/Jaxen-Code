"""S13 background command execution and completion notifications."""
import threading
from collections.abc import Callable


SLOW_KEYWORDS = (
    "install", "build", "test", "deploy", "compile",
    "docker build", "pip install", "npm install", "cargo build", "pytest", "make",
)

_counter = 0
_tasks: dict[str, dict] = {}
_results: dict[str, str] = {}
_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """Heuristic fallback for commands that are likely to take a while."""
    if tool_name != "run_bash":
        return False
    command = tool_input.get("command", "").lower()
    return any(keyword in command for keyword in SLOW_KEYWORDS)

# 两种方式启动后台运行
def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """Prefer the model's explicit request, then use the slow-command heuristic."""
    return bool(tool_input.get("run_in_background")) or is_slow_operation(tool_name, tool_input)


def start_background_task(command: str, runner: Callable[[], str]) -> str:
    """Run runner in a daemon thread and immediately return a background ID."""
    global _counter
    # 在锁内生成 ID 并记录状态
    with _lock:
        _counter += 1
        background_id = f"bg_{_counter:04d}"
        _tasks[background_id] = {"command": command, "status": "running"}

    def worker() -> None:
        status = "completed"
        try:
            result = runner()
        except Exception as error:
            status = "failed"
            result = f"后台任务执行失败: {error}"
        with _lock:
            _tasks[background_id]["status"] = status
            _results[background_id] = str(result)

    threading.Thread(target=worker, daemon=True).start()
    return background_id

# 收集后台工作的结果
def collect_background_results() -> list[str]:
    """Remove finished tasks and return one notification for each result."""
    with _lock:
        ready_ids = [task_id for task_id, task in _tasks.items()
                     if task["status"] in ("completed", "failed")]
        finished = [
            (task_id, _tasks.pop(task_id), _results.pop(task_id, ""))
            for task_id in ready_ids
        ]

    notifications = []
    for task_id, task, output in finished:
        notifications.append(
            "<task_notification>\n"
            f"  <task_id>{task_id}</task_id>\n"
            f"  <status>{task['status']}</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{output[:2000]}</summary>\n"
            "</task_notification>"
        )
    return notifications
