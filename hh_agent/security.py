"""
security.py — 路径沙箱、权限闸门、Hook 引擎
"""
from pathlib import Path

from .config import WORKDIR


# ═══════════════════════════════════════════════════════
#  路径沙箱 —— 所有文件操作必须过的门
# ═══════════════════════════════════════════════════════
def safe_path(p: str) -> Path:
    """将相对路径解析到工作区内，越界抛 ValueError。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径越出工作区: {p}")
    return path


# ═══════════════════════════════════════════════════════
#  闸门 1 —— 硬拒绝黑名单
# ═══════════════════════════════════════════════════════
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

def check_deny_list(command: str) -> str | None:
    """命中黑名单返回拦截原因，否则 None。"""
    for kw in DENY_LIST:
        if kw in command:
            return f"Blocked: '{kw}' 在命令黑名单里"
    return None

# ═══════════════════════════════════════════════════════
#  闸门 2 —— 规则匹配
# ═══════════════════════════════════════════════════════
PERMISSION_RULES = [
    {
        "tools": ["write_file", "edit_file"],
        "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
        "message": "写到工作区外了",
    },
    {
        "tools": ["run_bash"],
        "check": lambda args: any(
            kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]
        ),
        "message": "可能有破坏性的命令",
    },
]


def check_rules(tool_name: str, args: dict) -> str | None:
    """逐条匹配规则，命中返回消息，否则 None。"""
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# ═══════════════════════════════════════════════════════
#  闸门 3 —— 用户确认
# ═══════════════════════════════════════════════════════
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    """规则命中后暂停，等用户输入 y/N。"""
    print(f"\n⚠  {reason}")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


# ═══════════════════════════════════════════════════════
#  Hook 引擎 —— 生命周期事件 + 回调链 + 短路语义
# ═══════════════════════════════════════════════════════
HOOKS: dict[str, list] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}


def register_hook(event: str, callback) -> None:
    """往某个事件挂一个回调函数。"""
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """触发事件所有回调。第一个非 None 返回值短路整条链。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# ═══════════════════════════════════════════════════════
#  内置 Hook 回调（挂到 PreToolUse / PostToolUse / Stop）
# ═══════════════════════════════════════════════════════

# 返回空，表示权限没有被拒绝，非空则表示被拒绝
def _permission_hook(name: str, args: dict):
    """闸门 1+2+3 合一：黑名单 → 规则 → 用户确认。"""
    if name == "run_bash":
        reason = check_deny_list(args.get("command", ""))
        if reason:
            print(f"\n⛔ {reason}")
            return "该命令在黑名单中，请换一种不删除文件的方式"
    reason = check_rules(name, args)
    if reason and ask_user(name, args, reason) == "deny":
        return "权限被用户拒绝"
    return None

# 打印hook日志，永远返回空
def _log_hook(name: str, args: dict):
    preview = str(list(args.values())[:2])[:60]
    print(f"\033[90m[HOOK] {name}({preview})\033[0m")
    return None


def _large_output_hook(name: str, args: dict, output: str):
    if len(str(output)) > 30000:
        print(f"\033[33m[HOOK] ⚠ {name} 输出过大: {len(str(output))} 字符\033[0m")
    return None


def _context_inject_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: 工作目录 {WORKDIR}\033[0m")
    return None


def _summary_hook(messages: list):
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: 本次共调用 {tool_count} 次工具\033[0m")
    return None


# ── 注册内置 Hook（模块加载时自动执行）────────────────
register_hook("UserPromptSubmit", _context_inject_hook)
register_hook("PreToolUse", _log_hook)
register_hook("PreToolUse", _permission_hook)
register_hook("PostToolUse", _large_output_hook)
register_hook("Stop", _summary_hook)