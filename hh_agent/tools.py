"""
tools.py — 工具实现、工具定义(OpenAI function schema)、分发表、子 Agent
"""
import glob as g
import json
import locale
import subprocess
from copy import deepcopy

from . import config
from .security import safe_path, trigger_hooks
from .skills_loader import load_skill
from .compaction import preprocess_context
from .task_system import (claim_task, complete_task, create_task, format_tasks,
                          get_task)
from .cron_scheduler import cancel_job, format_jobs, schedule_job


# ═══════════════════════════════════════════════════════
#  工具实现
# ═══════════════════════════════════════════════════════
def run_bash(command: str, run_in_background: bool = False) -> str:
    """在工作目录下执行 shell 命令，返回 stdout+stderr。"""
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=config.WORKDIR,
            capture_output=True,
            timeout=120,
        )
        enc = locale.getpreferredencoding(False)
        out = (r.stdout + r.stderr).decode(enc, errors="replace").strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误，超时（120s）"
    except Exception as e:
        return f"命令执行失败：{e}"


def run_read(path: str, limit: int | None = None) -> str:
    """读取文件内容，可指定只读前 N 行。"""
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"...(还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)
    except Exception as e:
        return f"读取失败: {e}"


def run_write(path: str, content: str) -> str:
    """写入文件，父目录不存在则自动创建。"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"写入成功: {path}({len(content)} 字节)"
    except Exception as e:
        return f"写入失败: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """将文件中第一处 old_text 替换为 new_text。"""
    try:
        fp = safe_path(path)
        text = fp.read_text(encoding="utf-8")
        if old_text not in text:
            return f"未找到要替换的文本: {path}"
        fp.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"编辑成功: {path}"
    except Exception as e:
        return f"编辑失败: {e}"


def run_glob(pattern: str) -> str:
    """用 glob 模式匹配文件，返回匹配路径列表。"""
    try:
        results = []
        for match in g.glob(pattern, root_dir=config.WORKDIR, recursive=True):
            if (config.WORKDIR / match).resolve().is_relative_to(config.WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"glob 错误: {e}"


# ── todo_write ────────────────────────────────────────
CURRENT_TODOS: list[dict] = []


def run_todo_write(todos: list) -> str:
    """更新全局任务清单并打印。"""
    global CURRENT_TODOS
    CURRENT_TODOS = todos
    icons = {"pending": " ", "in_progress": "▸", "completed": "✓"}
    lines = ["\n## 当前任务"]
    for t in CURRENT_TODOS:
        lines.append(f"  [{icons.get(t.get('status'), '?')}] {t.get('content', '')}")
    print("\n".join(lines))
    return "\n".join(lines) + f"\n(已更新 {len(CURRENT_TODOS)} 个任务)"


# ── S12 持久化任务系统 ────────────────────────────────
def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    return f"已创建 {task.id}: {task.subject}"

def run_list_tasks() -> str:
    return format_tasks()


def run_get_task(task_id: str) -> str:
    return get_task(task_id)


def run_claim_task(task_id: str, owner: str = "agent") -> str:
    return claim_task(task_id, owner)


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


# ── S14 cron 调度工具 ─────────────────────────────────
def run_schedule_cron(cron: str, prompt: str, recurring: bool = True,
                      durable: bool = True) -> str:
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"创建定时任务失败: {result}"
    return f"已创建 {result.id}: '{result.cron}' -> {result.prompt}"


def run_list_crons() -> str:
    return format_jobs()


def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


# ═══════════════════════════════════════════════════════
#  子 Agent
# ═══════════════════════════════════════════════════════
def spawn_subagent(description: str) -> str:
    """派生子 Agent：独立上下文、受限工具集、只回传最终文本。"""
    messages = [
        {"role": "system", "content": config.SUB_SYSTEM},
        {"role": "user", "content": description},
    ]
    for _ in range(30):  # 安全上限
        preprocess_context(messages)
        response = config.client.chat.completions.create(
            model=config.MODEL,
            messages=messages,
            tools=SUB_TOOLS,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content})
            break

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
                blocked = trigger_hooks("PreToolUse", name, args)
                if blocked:
                    result = f"权限被拒绝: {blocked}"
                else:
                    handler = SUB_HANDLERS.get(name)
                    result = handler(**args) if handler else f"未知工具: {name}"
                    trigger_hooks("PostToolUse", name, args, result)
            except Exception as e:
                result = f"工具执行出错:{e}"
            print(f"\033[35m  [sub] > {name}\033[0m {str(result)[:120]}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # 只回传最后一条 assistant 文本
    for m in reversed(messages):
        if m["role"] == "assistant" and m.get("content"):
            return m["content"]
    return "(子 Agent 未产出文本结论)"


# ═══════════════════════════════════════════════════════
#  工具定义（OpenAI function schema）
# ═══════════════════════════════════════════════════════
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "读取指定路径的文件内容",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "limit": {"type": "integer", "description": "只读前 N 行(可选)"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "将内容写入指定路径的文件",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "要写入的内容"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file", "description": "把文件中的指定文本替换一次",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_text": {"type": "string", "description": "要被替换的原文本"},
            "new_text": {"type": "string", "description": "替换成的新文本"}},
            "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {
        "name": "run_bash", "description": "执行一条 shell 命令,返回 stdout/stderr",
        "parameters": {"type": "object",
                       "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "run_in_background": {"type": "boolean",
                                  "description": "耗时命令设为 true，立即返回任务 ID，完成结果稍后通知"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "glob", "description": "Find files matching a glob pattern.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "todo_write",
        "description": "创建并管理任务清单。开始多步任务前先列出全部步骤(status=pending)；"
                       "动手某步时改为 in_progress；完成后改为 completed。每次状态变化都重新调用本工具传入完整列表。",
        "parameters": {"type": "object", "properties": {
            "todos": {"type": "array", "items": {"type": "object", "properties": {
                "content": {"type": "string", "description": "任务描述"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
            }, "required": ["content", "status"]}}},
            "required": ["todos"]}}},
    {"type": "function", "function": {
        "name": "create_task",
        "description": "创建一个跨会话持久化的项目任务，可用 blockedBy 声明前置任务 ID。它不同于当前会话的 todo_write。",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string", "description": "简短任务标题"},
            "description": {"type": "string", "description": "完整任务说明"},
            "blockedBy": {"type": "array", "items": {"type": "string"},
                          "description": "必须先完成的任务 ID"}},
            "required": ["subject"]}}},
    {"type": "function", "function": {
        "name": "list_tasks", "description": "列出所有持久化任务的状态、负责人和依赖。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_task", "description": "按 ID 查看一个持久化任务的完整信息。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "claim_task", "description": "认领一个未被依赖阻塞的 pending 任务，并改为 in_progress。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "owner": {"type": "string", "description": "认领者名称，默认 agent"}},
            "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "complete_task", "description": "完成一个 in_progress 任务，并报告新解锁的下游任务。",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "schedule_cron", "description": "创建定时任务。cron 为五段式：分钟 小时 日期 月份 星期。",
        "parameters": {"type": "object", "properties": {
            "cron": {"type": "string", "description": "例如 */5 * * * *"},
            "prompt": {"type": "string", "description": "触发时交给主 Agent 的工作内容"},
            "recurring": {"type": "boolean", "description": "true 周期执行，false 触发一次后删除"},
            "durable": {"type": "boolean", "description": "true 写入磁盘并在重启后恢复"}},
            "required": ["cron", "prompt"]}}},
    {"type": "function", "function": {
        "name": "list_crons", "description": "列出当前所有定时任务。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "cancel_cron", "description": "按 ID 取消一个定时任务。",
        "parameters": {"type": "object", "properties": {
            "job_id": {"type": "string"}}, "required": ["job_id"]}}},
    {"type": "function", "function": {
        "name": "task",
        "description": "派一个子 Agent 独立完成一个复杂子任务。它有自己独立的对话上下文,"
                       "做完后只把结论文本回传给你,中间过程不进入你的上下文。"
                       "适合那种要读很多文件/试很多步、但你只关心最终结论的子任务,"
                       "比如'读懂某个模块并总结'、'在整个项目里定位某个 bug 的根因'。",
        "parameters": {"type": "object", "properties": {
            "description": {"type": "string", "description": "交给子 Agent 的任务,写清目标和期望产出"}},
            "required": ["description"]}}},
    {"type": "function", "function": {
        "name": "load_skill",
        "description": "加载某个技能的完整说明书。先看 system 里的技能目录,需要哪个就 load 哪个。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "技能名,必须是目录里列出的"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "compact",
        "description": "当你觉得上下文太长、想清理冗余历史时主动调用,会把历史总结压缩。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]

# 子 Agent 没有独立通知循环，因此 bash 保持同步，也不向它暴露后台参数。
SUB_TOOLS = deepcopy([
    tool for tool in TOOLS
    if tool["function"]["name"] not in (
        "task", "todo_write", "compact", "schedule_cron", "list_crons", "cancel_cron"
    )
])
for tool in SUB_TOOLS:
    if tool["function"]["name"] == "run_bash":
        tool["function"]["parameters"]["properties"].pop("run_in_background", None)

# ═══════════════════════════════════════════════════════
#  分发表 —— 名字 → 真函数
# ═══════════════════════════════════════════════════════
TOOL_HANDLERS = {
    "run_bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write": run_todo_write,
    "create_task": run_create_task,
    "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task,
    "complete_task": run_complete_task,
    "schedule_cron": run_schedule_cron,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
    "task": spawn_subagent,
    "load_skill": load_skill,
}

SUB_HANDLERS = {
    "run_bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "load_skill": load_skill,
    "create_task": run_create_task,
    "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}

# ── todo_write 提醒阈值：连续 N 轮不调 todo 就塞提醒 ──
NAG_THRESHOLD = 3
