"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import shutil
import subprocess
import textwrap
from functools import partial

from .workspace import IGNORED_PATH_NAMES
#
# schema       参数说明
# risky        是否需要危险操作审批
# description  告诉模型这个工具做什么
BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'"},
        "risky": False,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": "int=200"},
        "risky": False,
        "description": "Read a UTF-8 file by line range.",
    },
    "search": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "risky": False,
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": "int=20", "background": "bool=False"},
        "risky": True,
        "description": "Run a shell command in the repo root.",
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "risky": True,
        "description": "Write a text file.",
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "risky": True,
        "description": "Replace one exact text block in a file.",
    },
}

DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}

TASK_TOOL_SPECS = {
    "create_task": {
        "schema": {"subject": "str", "description": "str=''", "blocked_by": "list[str]=[]"},
        "risky": False,
        "description": "Create a durable project task with optional dependencies.",
    },
    "list_tasks": {
        "schema": {},
        "risky": False,
        "description": "List durable project tasks.",
    },
    "claim_task": {
        "schema": {"task_id": "str", "owner": "str='agent'"},
        "risky": False,
        "description": "Claim a pending durable task when dependencies are complete.",
    },
    "complete_task": {
        "schema": {"task_id": "str"},
        "risky": False,
        "description": "Complete an in-progress durable task and show newly unblocked work.",
    },
}

CRON_TOOL_SPECS = {
    "schedule_cron": {
        "schema": {"cron": "str", "prompt": "str", "recurring": "bool=True", "durable": "bool=True"},
        "risky": False,
        "description": "Schedule a future agent prompt with a 5-field cron expression.",
    },
    "list_crons": {
        "schema": {},
        "risky": False,
        "description": "List scheduled agent prompts.",
    },
    "cancel_cron": {
        "schema": {"job_id": "str"},
        "risky": False,
        "description": "Cancel a scheduled agent prompt.",
    },
}


def legal_tool_names():
    return set(BASE_TOOL_SPECS) | set(TASK_TOOL_SPECS) | set(CRON_TOOL_SPECS) | {"delegate"}

# 模型格式出错时给出示范
TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
    "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "run_shell_background": '<tool>{"name":"run_shell","args":{"command":"pytest -q","timeout":120,"background":true}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
    "create_task": '<tool>{"name":"create_task","args":{"subject":"Add tests","description":"Cover parser edge cases"}}</tool>',
    "claim_task": '<tool>{"name":"claim_task","args":{"task_id":"task_123_aaaa","owner":"main-agent"}}</tool>',
    "complete_task": '<tool>{"name":"complete_task","args":{"task_id":"task_123_aaaa"}}</tool>',
    "schedule_cron": '<tool>{"name":"schedule_cron","args":{"cron":"0 9 * * *","prompt":"Review open project tasks"}}</tool>',
}

# 把说明和执行函数绑在一起
def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    tools.update({
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
        for name, spec in TASK_TOOL_SPECS.items()
    })
    # 只有主 Agent 才能看到 Cron 工具
    if context.depth == 0:
        tools.update({
            name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
            for name, spec in CRON_TOOL_SPECS.items()
        })
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if context.depth < context.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, context)}
    return tools


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")

# 执行前的参数与资源校验
def validate_tool(context, name, args):
    args = args or {}

    if name == "list_files":
        path = context.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        context.path(args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "create_task":
        if not str(args.get("subject", "")).strip():
            raise ValueError("subject must not be empty")
        blocked_by = args.get("blocked_by", [])
        if blocked_by is None:
            return
        if not isinstance(blocked_by, list):
            raise ValueError("blocked_by must be a list")
        return

    if name == "list_tasks":
        return

    if name == "claim_task":
        if not str(args.get("task_id", "")).strip():
            raise ValueError("task_id must not be empty")
        return

    if name == "complete_task":
        if not str(args.get("task_id", "")).strip():
            raise ValueError("task_id must not be empty")
        return

    if name == "schedule_cron":
        if not str(args.get("cron", "")).strip():
            raise ValueError("cron must not be empty")
        if not str(args.get("prompt", "")).strip():
            raise ValueError("prompt must not be empty")
        return

    if name == "list_crons":
        return

    if name == "cancel_cron":
        if not str(args.get("job_id", "")).strip():
            raise ValueError("job_id must not be empty")
        return

    if name == "write_file":
        path = context.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        if context.depth >= context.max_depth:
            raise ValueError("delegate depth exceeded")
        return


# 开始工具类具体实现
def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(context.root)}\n{body}"


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        result = subprocess.run(
            ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
            cwd=context.root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no matches)"

    matches = []
    files = [path] if path.is_file() else [
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(context.root).parts)
    ]
    for file_path in files:
        for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(context.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    if context.background_tasks and context.background_tasks.should_run_background("run_shell", args):
        clean_args = {**args, "background": False}
        background_id = context.background_tasks.start(
            command,
            lambda: tool_run_shell(context, clean_args),
        )
        return f"background task {background_id} started for: {command}"
    result = subprocess.run(
        command,
        cwd=context.root,   # 默认工作路径
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
        # 目的是减少敏感信息被意外带进命令执行环境的风险。
        env=context.shell_env(),
    )
    return textwrap.dedent(
        f"""\
        exit_code: {result.returncode}
        stdout:
        {result.stdout.strip() or "(empty)"}
        stderr:
        {result.stderr.strip() or "(empty)"}
        """
    ).strip()


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(context.root)} ({len(content)} chars)"


def tool_patch_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return f"patched {path.relative_to(context.root)}"


def tool_delegate(context, args):
    if context.depth >= context.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    return context.spawn_delegate(args)


def tool_create_task(context, args):
    task = context.task_store.create(
        str(args["subject"]),
        str(args.get("description", "")),
        list(args.get("blocked_by") or []),
    )
    return f"created {task.id}: {task.subject}"


def tool_list_tasks(context, args):
    return context.task_store.render()


def tool_claim_task(context, args):
    return context.task_store.claim(str(args["task_id"]), str(args.get("owner", "agent")))


def tool_complete_task(context, args):
    return context.task_store.complete(str(args["task_id"]))


def tool_schedule_cron(context, args):
    result = context.cron_scheduler.schedule(
        str(args["cron"]),
        str(args["prompt"]),
        bool(args.get("recurring", True)),
        bool(args.get("durable", True)),
    )
    if isinstance(result, str):
        return f"error: {result}"
    return f"scheduled {result.id}: '{result.cron}' -> {result.prompt}"


def tool_list_crons(context, args):
    return context.cron_scheduler.render()


def tool_cancel_cron(context, args):
    return context.cron_scheduler.cancel(str(args["job_id"]))

# 工具分发表
_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "create_task": tool_create_task,
    "list_tasks": tool_list_tasks,
    "claim_task": tool_claim_task,
    "complete_task": tool_complete_task,
    "schedule_cron": tool_schedule_cron,
    "list_crons": tool_list_crons,
    "cancel_cron": tool_cancel_cron,
}
