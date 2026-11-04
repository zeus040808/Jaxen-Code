"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import json
import locale
import os
import re
import subprocess
import ast
import difflib
import fnmatch
import textwrap
from functools import partial

from .workspace import IGNORED_PATH_NAMES, MAX_TOOL_OUTPUT

# run_shell 单条命令的超时上限（秒）。装依赖、跑完整测试集常常要几分钟。
MAX_SHELL_TIMEOUT = 600
# read_file 默认读到第几行；输出过长时按行截断并提示从哪一行继续。
DEFAULT_READ_END = 2000
# 编辑后回显的 diff 最多几行，够模型确认改动即可。
MAX_DIFF_LINES = 60
# glob 最多返回多少个路径。
MAX_GLOB_RESULTS = 200
# search：默认/最多返回多少条结果；跳过超过这个大小的文件（数据、打包产物）。
DEFAULT_SEARCH_RESULTS = 100
MAX_SEARCH_RESULTS = 500
MAX_SEARCH_FILE_BYTES = 2_000_000
SEARCH_MODES = ("content", "files", "count")

#
# schema       参数说明
# risky        是否需要危险操作审批
# description  告诉模型这个工具做什么
BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'", "recursive": "bool=False", "max_depth": "int=3"},
        "risky": False,
        "description": "List workspace files. Use this instead of shell dir/ls.",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": f"int={DEFAULT_READ_END}"},
        "risky": False,
        "description": "Read one UTF-8 workspace file with line numbers. Long output is cut at a line boundary and says where to continue. Use this instead of shell type/cat/Get-Content.",
    },
    "read_files": {
        "schema": {"paths": "list[str]", "start": "int=1", "end": "int=300"},
        "risky": False,
        "description": "Read up to 8 UTF-8 workspace files in one call for comparison or synthesis.",
    },
    "search": {
        "schema": {
            "pattern": "str", "path": "str='.'", "glob": "str=''", "output_mode": "str='content'",
            "context": "int=0", "ignore_case": "bool=False", "max_results": f"int={DEFAULT_SEARCH_RESULTS}",
        },
        "risky": False,
        "description": (
            "Regex search over file contents. glob limits files (e.g. '*.py', 'django/db/**/*.py'). "
            "output_mode: 'files' lists matching files (start here to narrow down), 'count' gives matches per file, "
            "'content' shows matching lines (default) with optional context lines. Case-insensitive when the "
            "pattern is all lowercase unless ignore_case is set."
        ),
    },
    "repo_map": {
        "schema": {"path": "str='.'", "pattern": "str=''"},
        "risky": False,
        "description": (
            "Python code structure without reading whole files. Without pattern: classes, methods and function "
            "signatures with line numbers for every .py file under path (use a subdirectory on large repos). "
            "With pattern (regex on names, e.g. 'floatformat' or '^save$'): only the matching definitions, "
            "i.e. a go-to-definition across the repository."
        ),
    },
    "glob": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "risky": False,
        "description": "Find files by glob pattern such as '**/*.py' or 'src/**/test_*.ts'; newest first.",
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": "int=120", "background": "bool=False"},
        "risky": True,
        "description": "Run a repo-root shell command. Use only when no dedicated file/search tool fits; approval is conditional on mutation risk.",
    },
    "runtime_info": {
        "schema": {},
        "risky": False,
        "description": "Show redacted provider, model, OS, shell, cwd, and repo root without reading secret config files.",
    },
    "todo_write": {
        "schema": {"todos": "list[object]"},
        "risky": False,
        "description": (
            "Replace the session todo list for multi-step work. Each item is "
            "{content, status} with status pending, in_progress, or completed; "
            "send the full list every time and keep one item in_progress."
        ),
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "risky": True,
        "description": "Write a text file. Overwriting an existing file requires reading it first; the result shows a diff.",
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "risky": True,
        "description": "Replace one exact text block in a file. If the file changed since you read it, read it again first. The result shows a diff.",
    },
}

# 子 agent 有自己的 Session 与上下文：大范围的读代码、找调用链交给它，只把结论带回主上下文。
DEFAULT_DELEGATE_STEPS = 20
MAX_DELEGATE_STEPS = 30
DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": f"int={DEFAULT_DELEGATE_STEPS}"},
    "risky": False,
    "description": (
        "Hand a self-contained, read-only investigation (find where something is implemented, trace a call path, "
        "survey how a module is used) to a child agent with its own context. It can list, glob, search and read "
        "files but not edit or run commands. Describe exactly what to find and what to report back."
    ),
}

TASK_TOOL_SPECS = {
    "create_task": {
        "schema": {
            "subject": "str",
            "description": "str=''",
            "blocked_by": "list[str]=[]",
            "acceptance_criteria": "list[str]=[]",
        },
        "risky": False,
        "description": "Create a durable subtask with dependencies and explicit acceptance criteria.",
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
        "schema": {"task_id": "str", "evidence": "list[str]=[]"},
        "risky": False,
        "description": "Complete an in-progress task with evidence for its acceptance criteria.",
    },
    "fail_task": {
        "schema": {"task_id": "str", "reason": "str", "evidence": "list[str]=[]"},
        "risky": False,
        "description": "Mark a task failed with a concrete reason and optional evidence.",
    },
    "block_task": {
        "schema": {"task_id": "str", "reason": "str"},
        "risky": False,
        "description": "Mark a task blocked by missing information, permission, or dependency.",
    },
    "record_evidence": {
        "schema": {
            "claim": "str",
            "evidence": "str",
            "verified": "bool=True",
            "task_id": "str=''",
        },
        "risky": False,
        "description": "Record evidence for a verified or still-unverified conclusion.",
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

MEMORY_TOOL_SPECS = {
    "memory_list": {
        "schema": {"namespace": "str=''"},
        "risky": False,
        "description": "List structured durable memories visible in the current workspace, including ids and provenance.",
    },
    "memory_update": {
        "schema": {
            "action": "str=add|replace|remove|batch",
            "namespace": "str=memory|user",
            "scope": "str=workspace|project|global_user",
            "kind": "str",
            "content": "str=''",
            "match": "str=''",
            "provenance": "str",
            "confidence": "float",
            "operations": "list[object]=[]",
        },
        "risky": True,
        "description": "Atomically add, correct, forget, or batch-edit curated durable memory. Changes apply to prompts next session.",
    },
    "session_search": {
        "schema": {
            "query": "str",
            "limit": "int=5",
            "include_tools": "bool=False",
            "session_id": "str=''",
        },
        "risky": False,
        "description": "Search archived sessions and return source-addressable historical evidence.",
    },
    "session_read": {
        "schema": {
            "session_id": "str",
            "start": "int=0",
            "limit": "int=20",
            "around_message_id": "int=0",
            "include_tools": "bool=True",
        },
        "risky": False,
        "description": "Read a chronological window from an archived session, optionally around a search-result message id.",
    },
}

SKILL_TOOL_SPECS = {
    "skills_list": {
        "schema": {},
        "risky": False,
        "description": "List available procedural skills by name and description; skill bodies are not loaded.",
    },
    "skill_view": {
        "schema": {"name": "str"},
        "risky": False,
        "description": "Load one matching SKILL.md on demand before following its procedure.",
    },
    "skill_manage": {
        "schema": {
            "action": "str=create|patch|delete",
            "name": "str",
            "description": "str=''",
            "content": "str=''",
            "old_text": "str=''",
            "new_text": "str=''",
        },
        "risky": True,
        "description": "Create, patch, or delete a reusable procedural skill. Store procedures here, not facts or task progress.",
    },
}


def legal_tool_names():
    return set(BASE_TOOL_SPECS) | set(TASK_TOOL_SPECS) | set(CRON_TOOL_SPECS) | set(MEMORY_TOOL_SPECS) | set(SKILL_TOOL_SPECS) | {"delegate"}

# 模型格式出错时给出示范
TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":".","recursive":false}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
    "read_files": '<tool>{"name":"read_files","args":{"paths":["a.py","b.py"],"start":1,"end":300}}</tool>',
    "search": '<tool>{"name":"search","args":{"pattern":"def floatformat","glob":"*.py","output_mode":"files"}}</tool>',
    "glob": '<tool>{"name":"glob","args":{"pattern":"**/*.py","path":"."}}</tool>',
    "repo_map": '<tool>{"name":"repo_map","args":{"pattern":"^floatformat$"}}</tool>',
    "todo_write": '<tool>{"name":"todo_write","args":{"todos":[{"content":"Reproduce the bug","status":"completed"},{"content":"Fix parser","status":"in_progress"}]}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "run_shell_background": '<tool>{"name":"run_shell","args":{"command":"pytest -q","timeout":120,"background":true}}</tool>',
    "runtime_info": '<tool>{"name":"runtime_info","args":{}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "delegate": '<tool>{"name":"delegate","args":{"task":"Find where session files are written and list the functions involved","max_steps":20}}</tool>',
    "create_task": '<tool>{"name":"create_task","args":{"subject":"Add tests","description":"Cover parser edge cases","acceptance_criteria":["pytest passes"]}}</tool>',
    "claim_task": '<tool>{"name":"claim_task","args":{"task_id":"task_123_aaaa","owner":"main-agent"}}</tool>',
    "complete_task": '<tool>{"name":"complete_task","args":{"task_id":"task_123_aaaa","evidence":["pytest: 12 passed"]}}</tool>',
    "fail_task": '<tool>{"name":"fail_task","args":{"task_id":"task_123_aaaa","reason":"required dependency is unavailable"}}</tool>',
    "block_task": '<tool>{"name":"block_task","args":{"task_id":"task_123_aaaa","reason":"waiting for the target file path"}}</tool>',
    "record_evidence": '<tool>{"name":"record_evidence","args":{"claim":"tests pass","evidence":"pytest: 12 passed","verified":true}}</tool>',
    "schedule_cron": '<tool>{"name":"schedule_cron","args":{"cron":"0 9 * * *","prompt":"Review open project tasks"}}</tool>',
    "memory_list": '<tool>{"name":"memory_list","args":{"namespace":"memory"}}</tool>',
    "memory_update": '<tool>{"name":"memory_update","args":{"action":"replace","namespace":"memory","match":"old fact","content":"corrected fact","provenance":"user_correction","confidence":1.0}}</tool>',
    "session_search": '<tool>{"name":"session_search","args":{"query":"database migration","limit":5,"include_tools":false}}</tool>',
    "session_read": '<tool>{"name":"session_read","args":{"session_id":"20260812-example","around_message_id":42,"limit":12,"include_tools":true}}</tool>',
    "skills_list": '<tool>{"name":"skills_list","args":{}}</tool>',
    "skill_view": '<tool>{"name":"skill_view","args":{"name":"release-checklist"}}</tool>',
    "skill_manage": '<tool>{"name":"skill_manage","args":{"action":"create","name":"release-checklist","description":"Verify and publish a release","content":"# Release checklist\\n\\n## Procedure\\n1. Run tests."}}</tool>',
}


TOOL_ALIASES = {
    "list_dir": "list_files",
    "read_multiple_files": "read_files",
    "get_runtime_info": "runtime_info",
}


def canonical_tool_name(name):
    name = str(name or "").strip()
    return TOOL_ALIASES.get(name, name)


def _schema_field(spec):
    text = str(spec)
    kind, _, qualifier = text.partition("=")
    kind = kind.strip()
    schema = {"type": "string"}
    if kind == "int":
        schema = {"type": "integer"}
    elif kind == "float":
        schema = {"type": "number"}
    elif kind == "bool":
        schema = {"type": "boolean"}
    elif kind == "list[str]":
        schema = {"type": "array", "items": {"type": "string"}}
    elif kind == "list[object]":
        schema = {"type": "array", "items": {"type": "object"}}
    if "|" in qualifier:
        schema["enum"] = qualifier.split("|")
    optional = bool(qualifier) and "|" not in qualifier
    return schema, optional


def provider_tool_schemas(tools):
    """Translate HH's small readable schemas into provider function schemas."""
    result = []
    for name, tool in tools.items():
        properties = {}
        required = []
        for field, spec in tool["schema"].items():
            field_schema, optional = _schema_field(spec)
            properties[field] = field_schema
            if not optional:
                required.append(field)
        parameters = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            parameters["required"] = required
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool["description"],
                    "parameters": parameters,
                },
            }
        )
    return result


_READ_ONLY_COMMANDS = re.compile(
    r"(?i)^\s*(cd(?:\s|$)|dir(?:\s|$)|type(?:\s|$)|where(?:\s|$)|"
    r"git\s+(status|log|diff|show)(?:\s|$)|rg(?:\s|$)|"
    r"get-childitem(?:\s|$)|get-content(?:\s|$)|select-string(?:\s|$)|"
    r"pwd(?:\s|$)|ls(?:\s|$)|cat(?:\s|$))"
)
_MUTATING_COMMAND_WORDS = re.compile(
    r"(?i)\b(del|erase|rmdir|move|copy|ren|rename|mkdir|set-content|"
    r"add-content|remove-item|move-item|copy-item|new-item|git\s+(add|commit|push|"
    r"checkout|switch|reset|clean|merge|rebase)|pip\s+install|npm\s+install)\b"
)


def shell_command_is_read_only(command):
    command = str(command or "").strip()
    if (
        not command
        or _MUTATING_COMMAND_WORDS.search(command)
        or re.search(r"(?i)(^|&&|(?<!&)\&(?!&)|;|\|)\s*(rd|md)(?:\s|$)", command)
        or any(token in command for token in (">", ">>"))
    ):
        return False
    # Every chained segment must be an explicitly known inspection command.
    segments = [part.strip() for part in re.split(r"&&|(?<!&)\&(?!&)|;|\|", command) if part.strip()]
    return bool(segments) and all(_READ_ONLY_COMMANDS.match(part) for part in segments)


def tool_requires_approval(name, args):
    name = canonical_tool_name(name)
    if name == "run_shell":
        return not shell_command_is_read_only((args or {}).get("command", ""))
    if name == "delegate":
        return bool(DELEGATE_TOOL_SPEC["risky"])
    for specs in (BASE_TOOL_SPECS, TASK_TOOL_SPECS, CRON_TOOL_SPECS, MEMORY_TOOL_SPECS, SKILL_TOOL_SPECS):
        if name in specs:
            return bool(specs[name]["risky"])
    return True


def _decode_output(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    data = bytes(value)
    encodings = ["utf-8-sig", locale.getpreferredencoding(False)]
    if os.name == "nt":
        encodings.extend(["mbcs", "gb18030"])
    for encoding in dict.fromkeys(encodings):
        try:
            return data.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")

# 把说明和执行函数绑在一起
def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # 持久化 task 工具会写 .hh/tasks；调查型子 agent 不应修改父任务状态。
    # core 工具集只保留编码工具，减少每轮暴露给模型的无关工具。
    if context.depth == 0 and context.toolset == "full":
        tools.update({
            name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
            for name, spec in TASK_TOOL_SPECS.items()
        })
        # 只有主 Agent 才能看到 Cron 工具
        tools.update({
            name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
            for name, spec in CRON_TOOL_SPECS.items()
        })
        tools.update({
            name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
            for name, spec in MEMORY_TOOL_SPECS.items()
        })
        tools.update({
            name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
            for name, spec in SKILL_TOOL_SPECS.items()
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
    name = canonical_tool_name(name)

    if name == "list_files":
        path = context.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        max_depth = int(args.get("max_depth", 3))
        if max_depth < 1 or max_depth > 8:
            raise ValueError("max_depth must be in [1, 8]")
        return

    if name == "read_file":
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", DEFAULT_READ_END))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "repo_map":
        if not context.path(args.get("path", ".")).exists():
            raise ValueError("path does not exist")
        try:
            re.compile(str(args.get("pattern", "") or ""))
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        return

    if name == "glob":
        if not str(args.get("pattern", "")).strip():
            raise ValueError("pattern must not be empty")
        if not context.path(args.get("path", ".")).is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_files":
        paths = args.get("paths")
        if not isinstance(paths, list) or not paths or len(paths) > 8:
            raise ValueError("paths must contain between 1 and 8 file paths")
        if len({str(path) for path in paths}) != len(paths):
            raise ValueError("paths must not contain duplicates")
        for raw_path in paths:
            if not str(raw_path).strip() or not context.path(raw_path).is_file():
                raise ValueError(f"path is not a file: {raw_path}")
        start = int(args.get("start", 1))
        end = int(args.get("end", 300))
        if start < 1 or end < start or end - start + 1 > 500:
            raise ValueError("line range must be valid and no larger than 500 lines")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        context.path(args.get("path", "."))
        if str(args.get("output_mode", "content")) not in SEARCH_MODES:
            raise ValueError(f"output_mode must be one of {list(SEARCH_MODES)}")
        if not 0 <= int(args.get("context", 0)) <= 10:
            raise ValueError("context must be in [0, 10]")
        if not 1 <= int(args.get("max_results", DEFAULT_SEARCH_RESULTS)) <= MAX_SEARCH_RESULTS:
            raise ValueError(f"max_results must be in [1, {MAX_SEARCH_RESULTS}]")
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 120))
        if timeout < 1 or timeout > MAX_SHELL_TIMEOUT:
            raise ValueError(f"timeout must be in [1, {MAX_SHELL_TIMEOUT}]")
        return

    if name == "runtime_info":
        return

    if name == "create_task":
        if not str(args.get("subject", "")).strip():
            raise ValueError("subject must not be empty")
        blocked_by = args.get("blocked_by", [])
        if blocked_by is None:
            return
        if not isinstance(blocked_by, list):
            raise ValueError("blocked_by must be a list")
        criteria = args.get("acceptance_criteria", [])
        if criteria is not None and not isinstance(criteria, list):
            raise ValueError("acceptance_criteria must be a list")
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
        if not isinstance(args.get("evidence", []), list):
            raise ValueError("evidence must be a list")
        return

    if name == "fail_task":
        if not str(args.get("task_id", "")).strip():
            raise ValueError("task_id must not be empty")
        if not str(args.get("reason", "")).strip():
            raise ValueError("reason must not be empty")
        if not isinstance(args.get("evidence", []), list):
            raise ValueError("evidence must be a list")
        return

    if name == "block_task":
        if not str(args.get("task_id", "")).strip():
            raise ValueError("task_id must not be empty")
        if not str(args.get("reason", "")).strip():
            raise ValueError("reason must not be empty")
        return

    if name == "todo_write":
        if not isinstance(args.get("todos"), list):
            raise ValueError("todos must be a list of {content, status} objects")
        return

    if name == "record_evidence":
        if not str(args.get("claim", "")).strip():
            raise ValueError("claim must not be empty")
        if not str(args.get("evidence", "")).strip():
            raise ValueError("evidence must not be empty")
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

    if name == "memory_list":
        namespace = str(args.get("namespace", "")).strip()
        if namespace and namespace not in {"memory", "user"}:
            raise ValueError("namespace must be memory, user, or empty")
        return

    if name == "memory_update":
        action = str(args.get("action", "")).strip().lower()
        if action not in {"add", "replace", "remove", "batch"}:
            raise ValueError("action must be add, replace, remove, or batch")
        if action == "batch":
            operations = args.get("operations")
            if not isinstance(operations, list) or not operations:
                raise ValueError("operations must be a non-empty list")
            return
        if action in {"replace", "remove"} and not str(args.get("match", "")).strip():
            raise ValueError("match must not be empty for replace/remove")
        if action in {"add", "replace"} and not str(args.get("content", "")).strip():
            raise ValueError("content must not be empty for add/replace")
        return

    if name == "session_search":
        if not str(args.get("query", "")).strip():
            raise ValueError("query must not be empty")
        limit = int(args.get("limit", 5))
        if limit < 1 or limit > 20:
            raise ValueError("limit must be in [1, 20]")
        return

    if name == "session_read":
        if not str(args.get("session_id", "")).strip():
            raise ValueError("session_id must not be empty")
        start = int(args.get("start", 0))
        limit = int(args.get("limit", 20))
        around_message_id = int(args.get("around_message_id", 0))
        if start < 0 or around_message_id < 0:
            raise ValueError("start and around_message_id must not be negative")
        if limit < 1 or limit > 50:
            raise ValueError("limit must be in [1, 50]")
        return

    if name == "skills_list":
        return

    if name == "skill_view":
        if not str(args.get("name", "")).strip():
            raise ValueError("name must not be empty")
        return

    if name == "skill_manage":
        action = str(args.get("action", "")).strip().lower()
        if action not in {"create", "patch", "delete"}:
            raise ValueError("action must be create, patch, or delete")
        if not str(args.get("name", "")).strip():
            raise ValueError("name must not be empty")
        if action == "create" and (
            not str(args.get("description", "")).strip()
            or not str(args.get("content", "")).strip()
        ):
            raise ValueError("create requires description and content")
        if action == "patch" and not str(args.get("old_text", "")):
            raise ValueError("patch requires old_text")
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
        max_steps = int(args.get("max_steps", DEFAULT_DELEGATE_STEPS))
        if max_steps < 1 or max_steps > MAX_DELEGATE_STEPS:
            raise ValueError(f"max_steps must be in [1, {MAX_DELEGATE_STEPS}]")
        return


# 开始工具类具体实现
def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    recursive = bool(args.get("recursive", False))
    max_depth = int(args.get("max_depth", 3))
    candidates = path.rglob("*") if recursive else path.iterdir()
    entries = []
    for item in candidates:
        relative_to_path = item.relative_to(path)
        if any(part in IGNORED_PATH_NAMES for part in relative_to_path.parts):
            continue
        if recursive and len(relative_to_path.parts) > max_depth:
            continue
        entries.append(item)
    entries.sort(key=lambda item: (item.is_file(), str(item.relative_to(path)).lower()))
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(context, args, char_limit=MAX_TOOL_OUTPUT - 300):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", DEFAULT_READ_END))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header = f"# {path.relative_to(context.root)}"
    # 按整行截断：代码不能走头尾截断，否则中间几百行会悄悄消失。
    out, used, last = [], len(header), start - 1
    for number, line in enumerate(lines[start - 1:end], start=start):
        rendered = f"{number:>4}: {line}"
        if out and used + len(rendered) + 1 > char_limit:
            break
        out.append(rendered)
        used += len(rendered) + 1
        last = number
    body = "\n".join(out)
    shown_end = min(end, len(lines))
    if last < shown_end:
        body += (
            f"\n...[showing lines {start}-{last} of {len(lines)}; "
            f"call read_file with start={last + 1} to continue]"
        )
    return f"{header}\n{body}"


def tool_read_files(context, args):
    start = int(args.get("start", 1))
    end = int(args.get("end", 300))
    sections = []
    per_file_budget = max(800, 11000 // len(args["paths"]))
    for raw_path in args["paths"]:
        sections.append(tool_read_file(
            context, {"path": raw_path, "start": start, "end": end}, char_limit=per_file_budget
        ))
    return "\n\n".join(sections)


def tool_runtime_info(context, args):
    del args
    info = context.runtime_info()
    keys = ("provider", "model", "platform", "shell", "cwd", "repo_root", "approval_policy", "read_only")
    return "\n".join(f"{key}: {info.get(key, '')}" for key in keys)


def _project_files(context, base):
    """base 下属于项目的文件。

    git 仓库里用 `git ls-files`（含未跟踪文件，遵守 .gitignore），和 rg 一样跳过依赖目录与构建产物；
    不是 git 仓库时遍历目录，只跳过固定的几个目录。
    """
    listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=base, capture_output=True)
    if listed.returncode == 0:
        names = [name for name in listed.stdout.decode("utf-8", errors="replace").split("\0") if name]
        return [base / name for name in sorted(names) if (base / name).is_file()]
    return [
        item for item in sorted(base.rglob("*"))
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(context.root).parts)
    ]


def _search_files(context, base, glob):
    if base.is_file():
        return [base]
    return [
        item for item in _project_files(context, base)
        # rg 的语义：'*.py' 匹配任意层级的文件名，含 '/' 的模式匹配相对路径。
        if (not glob or fnmatch.fnmatch(item.name, glob) or fnmatch.fnmatch(str(item.relative_to(base)), glob))
        and item.stat().st_size <= MAX_SEARCH_FILE_BYTES
    ]


def tool_search(context, args):
    pattern = str(args["pattern"]).strip()
    # smart-case：全小写的模式不区分大小写，含大写时区分（与 rg/Claude Code 一致）。
    ignore_case = bool(args["ignore_case"]) if "ignore_case" in args else pattern == pattern.lower()
    regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    mode = str(args.get("output_mode", "content"))
    around = int(args.get("context", 0))
    limit = int(args.get("max_results", DEFAULT_SEARCH_RESULTS))
    lines_out, total = [], 0
    for file_path in _search_files(context, context.path(args.get("path", ".")), str(args.get("glob", "") or "")):
        raw = file_path.read_bytes()
        if b"\0" in raw[:1024]:
            continue  # 二进制文件
        lines = raw.decode("utf-8", errors="replace").splitlines()
        hits = [index for index, line in enumerate(lines) if regex.search(line)]
        if not hits:
            continue
        relative = file_path.relative_to(context.root)
        if mode != "content":
            total += 1
            if total <= limit:
                lines_out.append(str(relative) if mode == "files" else f"{relative}:{len(hits)}")
            continue
        for index in hits:
            total += 1
            if total > limit:
                continue
            if not around:
                lines_out.append(f"{relative}:{index + 1}:{lines[index]}")
                continue
            # 带上下文：匹配行用 ':'，上下文行用 '-'，块之间用 '--' 分隔（与 grep 一致）。
            for number in range(max(0, index - around), min(len(lines), index + around + 1)):
                mark = ":" if number == index else "-"
                lines_out.append(f"{relative}{mark}{number + 1}{mark}{lines[number]}")
            lines_out.append("--")
    if total > limit:
        lines_out.append(f"...[{total - limit} more results; narrow with path/glob or use output_mode='files']")
    return "\n".join(lines_out) or "(no matches)"


def _glob_match(relative, pattern):
    """Path.glob 的语义：不含 '/' 的模式只匹配当前目录；'**/' 可以匹配零层或多层目录。"""
    if "/" not in pattern:
        return "/" not in relative and fnmatch.fnmatch(relative, pattern)
    return fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative, pattern.replace("**/", ""))


def tool_glob(context, args):
    pattern = str(args.get("pattern", "")).strip()
    base = context.path(args.get("path", "."))
    matches = []
    for item in _project_files(context, base):
        if _glob_match(str(item.relative_to(base)), pattern):
            matches.append((item.stat().st_mtime, str(item.relative_to(context.root))))
    # 最近改过的文件最可能和当前任务有关，排在前面。
    matches.sort(key=lambda pair: (-pair[0], pair[1]))
    lines = [path for _, path in matches[:MAX_GLOB_RESULTS]]
    if len(matches) > MAX_GLOB_RESULTS:
        lines.append(f"...[{len(matches) - MAX_GLOB_RESULTS} more matches; narrow the pattern]")
    return "\n".join(lines) or "(no matches)"


# 文件路径 -> (修改时间, 符号列表)。同一进程里重复调用 repo_map 不必重新解析。
_SYMBOL_CACHE = {}


def _python_symbols(file_path):
    """[(行号, 缩进层级, 名字, 签名)]：顶层类与函数，以及类里的方法。"""
    mtime = file_path.stat().st_mtime
    cached = _SYMBOL_CACHE.get(file_path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        symbols = []
    else:
        symbols = []
        for node in tree.body:
            symbols += _describe(node, 0)
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    symbols += _describe(child, 1)
    _SYMBOL_CACHE[file_path] = (mtime, symbols)
    return symbols


def _describe(node, level):
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(base) for base in node.bases)
        return [(node.lineno, level, node.name, f"class {node.name}({bases})" if bases else f"class {node.name}")]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return [(node.lineno, level, node.name, f"{prefix} {node.name}({ast.unparse(node.args)})")]
    return []


def tool_repo_map(context, args):
    base = context.path(args.get("path", "."))
    pattern = str(args.get("pattern", "") or "")
    regex = re.compile(pattern) if pattern else None
    files = [base] if base.is_file() else _project_files(context, base)
    files = [item for item in files if item.suffix == ".py"]
    if not files:
        return "(no Python files here; repo_map only understands Python, use search for other languages)"
    out, used, skipped = [], 0, 0
    budget = MAX_TOOL_OUTPUT - 300
    for file_path in files:
        relative = file_path.relative_to(context.root)
        symbols = _python_symbols(file_path)
        if regex:
            # 查定义：每个匹配一行，带路径，方便直接 read_file 跳过去。
            block = [f"{relative}:{line}: {signature}" for line, _, name, signature in symbols if regex.search(name)]
        else:
            block = [str(relative)] + [f"{'    ' * (level + 1)}{line}: {signature}" for line, level, _, signature in symbols]
        if not block:
            continue
        size = sum(len(item) + 1 for item in block)
        if used + size > budget:
            skipped += 1
            continue
        out += block
        used += size
    if skipped:
        out.append(f"...[{skipped} more files not shown; narrow path or add a pattern]")
    return "\n".join(out) or "(no matching definitions)"


def _edit_diff(relative_path, before, after):
    diff = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{relative_path}", tofile=f"b/{relative_path}", lineterm="", n=2,
    ))
    if len(diff) > MAX_DIFF_LINES:
        diff = diff[:MAX_DIFF_LINES] + [f"...[{len(diff) - MAX_DIFF_LINES} more diff lines]"]
    return "\n".join(diff) or "(no textual change)"


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 120))
    if timeout < 1 or timeout > MAX_SHELL_TIMEOUT:
        raise ValueError(f"timeout must be in [1, {MAX_SHELL_TIMEOUT}]")
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
        text=False,
        timeout=timeout,
        # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
        # 目的是减少敏感信息被意外带进命令执行环境的风险。
        env=context.shell_env(),
    )
    stdout = _decode_output(result.stdout).strip()
    stderr = _decode_output(result.stderr).strip()
    return textwrap.dedent(
        f"""\
        exit_code: {result.returncode}
        stdout:
        {stdout or "(empty)"}
        stderr:
        {stderr or "(empty)"}
        """
    ).strip()


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    before = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    relative = path.relative_to(context.root)
    if before is None:
        return f"created {relative} ({len(content)} chars)"
    return f"wrote {relative} ({len(content)} chars)\n" + _edit_diff(relative, before, content)


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
    updated = text.replace(old_text, str(args["new_text"]), 1)
    path.write_text(updated, encoding="utf-8")
    relative = path.relative_to(context.root)
    return f"patched {relative}\n" + _edit_diff(relative, text, updated)


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
        list(args.get("acceptance_criteria") or []),
    )
    return f"created {task.id}: {task.subject}"


def tool_list_tasks(context, args):
    return context.task_store.render()


def tool_claim_task(context, args):
    return context.task_store.claim(str(args["task_id"]), str(args.get("owner", "agent")))


def tool_complete_task(context, args):
    return context.task_store.complete(str(args["task_id"]), list(args.get("evidence") or []))


def tool_fail_task(context, args):
    return context.task_store.fail(
        str(args["task_id"]),
        str(args["reason"]),
        list(args.get("evidence") or []),
    )


def tool_block_task(context, args):
    return context.task_store.block(str(args["task_id"]), str(args["reason"]))


def tool_todo_write(context, args):
    return context.task_state.set_todos(args["todos"])


def tool_record_evidence(context, args):
    task_id = str(args.get("task_id", "")).strip()
    evidence = str(args["evidence"])
    verified = bool(args.get("verified", True))
    if task_id and verified:
        context.task_store.add_evidence(task_id, evidence)
    return context.task_state.record_claim(
        claim=str(args["claim"]),
        evidence=evidence,
        verified=verified,
        task_id=task_id,
    )


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


def tool_memory_list(context, args):
    entries = context.curated_memory.entries(namespace=str(args.get("namespace", "")).strip() or None)
    if not entries:
        return "(no curated durable memories)"
    return "\n".join(
        f"[{entry.id}] namespace={entry.namespace} scope={entry.scope} kind={entry.kind} "
        f"provenance={entry.provenance} confidence={entry.confidence:.2f} version={entry.version}\n"
        f"{entry.content}"
        for entry in entries
    )


def tool_memory_update(context, args):
    action = str(args.get("action", "")).strip().lower()
    if action == "batch":
        operations = [dict(operation) for operation in args.get("operations", [])]
    else:
        operations = [{key: value for key, value in args.items() if key != "operations"}]
    for operation in operations:
        operation.setdefault("source_session_id", context.session_id)
    results = context.curated_memory.apply_batch(operations)
    return (
        json.dumps(results, ensure_ascii=False, sort_keys=True)
        + "\nPersisted atomically. The frozen prompt snapshot is unchanged; this becomes visible next session."
    )


def tool_session_search(context, args):
    results = context.session_archive.search(
        str(args["query"]),
        limit=int(args.get("limit", 5)),
        include_tools=bool(args.get("include_tools", False)),
        session_id=str(args.get("session_id", "")).strip(),
    )
    return context.session_archive.render_results(results)


def tool_session_read(context, args):
    results = context.session_archive.read(
        str(args["session_id"]),
        start=int(args.get("start", 0)),
        limit=int(args.get("limit", 20)),
        around_message_id=int(args.get("around_message_id", 0)),
        include_tools=bool(args.get("include_tools", True)),
    )
    return context.session_archive.render_results(results)


def tool_skills_list(context, args):
    return context.skill_store.render_index()


def tool_skill_view(context, args):
    return context.skill_store.read(str(args["name"]))


def tool_skill_manage(context, args):
    action = str(args["action"]).strip().lower()
    name = str(args["name"]).strip()
    if action == "create":
        path = context.skill_store.create(
            name,
            str(args["description"]),
            str(args["content"]),
        )
        return f"created skill: {name}\npath: {path}\nIt becomes visible in prompt indexes next session."
    if action == "patch":
        path = context.skill_store.patch(name, str(args["old_text"]), str(args.get("new_text", "")))
        return f"patched skill: {name}\npath: {path}\nThe frozen index is unchanged until next session."
    path = context.skill_store.delete(name)
    return f"deleted skill: {name}\npath: {path}"

# 工具分发表
_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "read_files": tool_read_files,
    "search": tool_search,
    "glob": tool_glob,
    "repo_map": tool_repo_map,
    "todo_write": tool_todo_write,
    "run_shell": tool_run_shell,
    "runtime_info": tool_runtime_info,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "create_task": tool_create_task,
    "list_tasks": tool_list_tasks,
    "claim_task": tool_claim_task,
    "complete_task": tool_complete_task,
    "fail_task": tool_fail_task,
    "block_task": tool_block_task,
    "record_evidence": tool_record_evidence,
    "schedule_cron": tool_schedule_cron,
    "list_crons": tool_list_crons,
    "cancel_cron": tool_cancel_cron,
    "memory_list": tool_memory_list,
    "memory_update": tool_memory_update,
    "session_search": tool_session_search,
    "session_read": tool_session_read,
    "skills_list": tool_skills_list,
    "skill_view": tool_skill_view,
    "skill_manage": tool_skill_manage,
}
