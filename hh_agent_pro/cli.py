"""命令行入口。

这个模块负责把“用户怎么启动 HH”翻译成 CodingAgent 需要的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import os
import shutil
import sys
import threading
import textwrap
import time

from .config import load_project_env, provider_env
from .providers import EvalScriptModelClient, OpenAIChatClient
from .agent import CodingAgent
from .session_store import SessionStore
from .terminal_ui import TerminalEventSink
from .workspace import WorkspaceContext, middle


WELCOME_ART = (
    "    __  __  __  __       ___                    __ ",
    "   / / / / / / / /      / _ | ___ ____ ___  ___/ /_",
    "  / /_/ / / /_/ /  ==  / __ |/ _ `/ -_) _ \\/ _  __/",
    " / __  / / __  /      /_/ |_|\\_, /\\__/_//_/\\_,_/   ",
    "/_/ /_/ /_/ /_/              /___/                  ",
)
WELCOME_NAME = "HH AGENT PRO"
WELCOME_SUBTITLE = "OpenAI-compatible coding agent harness"
WELCOME_STATUS = "workspace armed | tools gated | traces on"
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /memory  Show the agent's distilled working memory.
    /skills  List learned procedural skills; use /skills <name> to read one.
    /model   Show or switch the active provider/model.
    /session Show the path to the saved session file.
    /tasks   Show durable project tasks.
    /crons   Show scheduled agent prompts.
    /reset   Clear the current session history and memory.
    /exit    Exit the agent.
    """
).strip()


DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
SECRET_ENV_NAMES_VAR = "HH_SECRET_ENV_NAMES"
# deepseek：DeepSeek 预设；openai：任意 OpenAI Chat Completions 兼容端点（通义、OpenAI 等）；
# fake：仅限隔离评测的脚本化模型。
PROVIDER_NAMES = ("deepseek", "openai", "fake")
# 每个 provider 从哪些环境变量取 key / base_url / model（按优先级）。
PROVIDER_ENV = {
    "deepseek": {
        "api_key": ("HH_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
        "base_url": ("HH_DEEPSEEK_API_BASE", "DEEPSEEK_API_BASE"),
        "model": ("HH_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"),
    },
    "openai": {
        "api_key": ("HH_OPENAI_API_KEY", "OPENAI_API_KEY"),
        "base_url": ("HH_OPENAI_API_BASE", "OPENAI_API_BASE"),
        "model": ("HH_OPENAI_MODEL", "OPENAI_MODEL"),
    },
}

# 决定这次 agent 到底用哪个模型名
# args：命令行参数对象
# provider：当前使用哪个模型服务商
def _effective_model(args, provider):
    # 优先级：命令行 --model > provider 对应的环境变量 > 代码默认值。
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "fake":
        return EvalScriptModelClient.model
    names = PROVIDER_ENV[provider]["model"]
    model = provider_env(names[0], names[1:])
    if model:
        return model
    if provider == "deepseek":
        return DEFAULT_DEEPSEEK_MODEL
    raise RuntimeError("Set --model or HH_OPENAI_MODEL for the openai provider.")

# 需要加密的环境变量名
DEFAULT_SECRET_ENV_NAMES = (
    "HH_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "HH_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "GITHUB_PAT",
    "GH_PAT",
)


# 整理哪些环境变量名应该被当作密钥隐藏/打码
def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    # 把用户命令行传入的额外密钥变量名也加进去
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    # 其他需要保护的环境变量名
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
        # 返回一个set
    return sorted(configured_secret_names)


def _build_model_client(args):
    provider = getattr(args, "provider", "deepseek")
    if provider == "fake":
        return EvalScriptModelClient()
    env = PROVIDER_ENV[provider]
    api_key = provider_env(env["api_key"][0], env["api_key"][1:])
    if not api_key:
        raise RuntimeError(f"{provider} API key is missing. Set {' or '.join(env['api_key'])} in the project .env file.")
    base_url = getattr(args, "base_url", None) or provider_env(
        env["base_url"][0], env["base_url"][1:], DEFAULT_DEEPSEEK_BASE_URL if provider == "deepseek" else ""
    )
    if not base_url:
        raise RuntimeError("Set --base-url or HH_OPENAI_API_BASE for the openai provider.")
    client = OpenAIChatClient(
        model=_effective_model(args, provider),
        base_url=base_url,
        api_key=api_key,
        temperature=args.temperature,
        timeout=args.request_timeout,
        context_window=getattr(args, "context_window", None),
        label="DeepSeek" if provider == "deepseek" else "Model",
    )
    client.provider = provider
    return client


def _model_status(agent):
    """Return the active provider/model without exposing credentials."""
    client = agent.model_client
    provider = getattr(client, "provider", client.__class__.__name__)
    model = getattr(client, "model", "unknown")
    return f"{provider} / {model}"


def _switch_model(agent, args, provider, model=None):
    """Build the replacement first, then atomically swap the active client.

    Session history and memory live on ``agent``, not on the provider client, so
    they survive the swap. A construction/configuration failure leaves the old
    client untouched.
    """
    provider = str(provider).strip().lower()
    if provider not in PROVIDER_NAMES:
        raise RuntimeError(
            f"Unknown provider: {provider}. Available: {', '.join(PROVIDER_NAMES)}"
        )
    switch_args = argparse.Namespace(**vars(args))
    switch_args.provider = provider
    switch_args.model = str(model).strip() if model else None
    switch_args.cwd = agent.workspace.repo_root
    # A command-line base URL belongs to the startup provider. Runtime switches
    # use the target provider's own environment configuration instead.
    switch_args.base_url = None
    replacement = _build_model_client(switch_args)
    old_status = _model_status(agent)
    old_client = agent.model_client
    agent.model_client = replacement
    if hasattr(agent, "refresh_prefix"):
        try:
            agent.refresh_prefix()
        except Exception:
            agent.model_client = old_client
            agent.refresh_prefix()
            raise
    args.provider = provider
    args.model = switch_args.model
    return f"model switched: {old_status} -> {_model_status(agent)}"


def build_welcome(agent, model, host):
    width = max(76, min(shutil.get_terminal_size((92, 20)).columns, 96))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width
    use_color = sys.stdout.isatty()

    def paint(text, code):
        if not use_color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    top = paint(divider("="), "36;1")
    split = paint(divider("-"), "36")
    bottom = paint(divider("="), "36;1")
    art_rows = [paint(center(text), "36;1") for text in WELCOME_ART]
    status = f"{WELCOME_STATUS} | provider-ready"
    command_hint = "commands: /help  /model  /memory  /tasks  /crons  /session  /exit"
    rows = [
        top,
        *art_rows,
        paint(center(WELCOME_NAME), "37;1"),
        paint(center(WELCOME_SUBTITLE), "36"),
        paint(center(status), "32"),
        split,
        row(""),
        row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
        pair("MODEL", model, "BRANCH", agent.workspace.branch),
        pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
        pair("RUNS", ".hh/runs", "MEMORY", ".hh/memory"),
        row(""),
        paint(center(command_hint), "90"),
        bottom,
    ]
    return "\n".join(rows)


def build_agent(args, event_sink=None, machine_mode=False):
    """根据 CLI 参数装配出一个可运行的 Agent 实例。

    为什么存在：
    命令行参数只是字符串和开关，CodingAgent 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `CodingAgent`，或一个从旧 Session 恢复出来的 `CodingAgent`

    在 agent 链路里的位置：
    它是整个程序启动链路里创建 Agent 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 Agent 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。、
    # 分配工作区和沙箱路径
    workspace = WorkspaceContext.build(args.cwd)
    # 加载环境变量
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)

    # 创建 SessionStore对象 sessions 文件夹（目录）
    store = SessionStore(workspace.repo_root + "/.hh/sessions")

    model = _build_model_client(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        # 从 SessionStore 恢复旧会话，再装配 CodingAgent。
        return CodingAgent.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            read_only=bool(getattr(args, "read_only", False)),
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            event_sink=event_sink,
            machine_mode=machine_mode,
            episode_timeout_ms=getattr(args, "episode_timeout_ms", 1_200_000),
            feature_flags={"skill_learning": not bool(getattr(args, "no_auto_skills", False))},
            toolset=getattr(args, "toolset", "core"),
        )
    # 没有恢复 ID 时，创建一个新 Agent。
    return CodingAgent(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        read_only=bool(getattr(args, "read_only", False)),
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        event_sink=event_sink,
        machine_mode=machine_mode,
        episode_timeout_ms=getattr(args, "episode_timeout_ms", 1_200_000),
        feature_flags={"skill_learning": not bool(getattr(args, "no_auto_skills", False))},
        toolset=getattr(args, "toolset", "core"),
    )

# 定义命令行参数规则
def build_arg_parser():
    # 这里创建一个命令行解析器
    parser = argparse.ArgumentParser(
        # 在 --help 里自动显示默认值
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="DeepSeek-default multi-model local coding agent harness.",
    )
    # 这是一个位置参数，不是 --xxx 形式
    # nargs="*" 的意思是：可以有 0 个、1 个、多个 prompt 片段
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")

    # --cwd 指定 Agent 工作在哪个目录
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--provider", choices=PROVIDER_NAMES, default="deepseek", help="Model backend to use. DeepSeek (deepseek-flash) is the default.")

    # --model 用来临时覆盖模型名
    parser.add_argument("--model", default=None, help="Model override.")
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible API base URL (required for --provider openai).")
    parser.add_argument("--context-window", type=int, default=None, help="Model context window in tokens, for models not in the built-in table.")
    parser.add_argument("--request-timeout", type=int, default=300, help="Model request timeout in seconds.")

    #用来恢复之前的 session（默认新建session）
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    # 控制危险工具是否需要审批
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Block every workspace-mutating tool (used by read-only eval tasks).",
    )
    # 哪些环境变量名应该被当作密钥，在 trace/report 里打码（110行使用）
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )

    parser.add_argument(
        "--toolset",
        choices=("core", "full"),
        default="core",
        help="core: coding tools only (default); full: also task/cron/memory/skill/session tools.",
    )
    parser.add_argument("--max-steps", type=int, default=50, help="Maximum effective tool calls per request (default: 50).")
    #限制模型每次最多输出多少 token
    parser.add_argument("--max-new-tokens", type=int, default=8192, help="Maximum model output tokens per step (default: 8192).")
    parser.add_argument("--episode-timeout-ms", type=int, default=1_200_000, help="Wall-clock budget for the complete episode (default: 20 min).")
    parser.add_argument("--jsonl", action="store_true", help="Read versioned machine requests from stdin and emit JSONL events/turns.")
    parser.add_argument("--no-auto-skills", action="store_true", help="Disable automatic SKILL.md deposition after verified complex runs.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature.")
    return parser


def main(argv=None):
    # 读取参数（把用户输入的命令行参数解析成一个对象）
    args = build_arg_parser().parse_args(argv)
    if args.jsonl:
        from .jsonl_protocol import run_jsonl_stdio

        return run_jsonl_stdio(args, build_agent)
    # 创建 Agent；后续每条输入分别调用一次 ask()。
    terminal = TerminalEventSink()
    agent = build_agent(args, event_sink=terminal)
    restored_crons = agent.cron_scheduler.start()

    # 上锁
    agent_lock = threading.Lock()

    model = _model_status(agent)
    host = getattr(agent.model_client, "base_url", "-")
    # 打印欢迎界面
    print(build_welcome(agent, model=model, host=host))
    if restored_crons:
        print(f"[cron] restored {restored_crons} scheduled prompt(s)")

    # 命令行参数包含prompt，即一次性回答
    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            while prompt:
                try:
                    with agent_lock:
                        result = agent.ask(prompt)
                        if not terminal.final_rendered:
                            print(result)
                except RuntimeError as exc:
                    if not terminal.final_rendered:
                        print(str(exc), file=sys.stderr)
                    return 1
                except KeyboardInterrupt:
                    print("\nInterrupted. Run state and checkpoint were saved.")
                    return 130
                if not terminal.needs_input:
                    break
                if not sys.stdin.isatty():
                    print("The agent requested user input, but stdin is not interactive.", file=sys.stderr)
                    return 2
                try:
                    prompt = input("\nchoice> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("")
                    return 2
        return 0

    # 定时系统
    def queue_processor_loop():
        while True:
            time.sleep(0.2)
            if not agent.cron_scheduler.has_queue() or not agent_lock.acquire(blocking=False):
                continue
            try:
                if agent.cron_scheduler.has_queue():
                    print("\n[cron] delivering scheduled prompt")
                    result = agent.ask("[cron tick]")
                    if not terminal.final_rendered:
                        print(result)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
            finally:
                agent_lock.release()

    threading.Thread(target=queue_processor_loop, daemon=True).start()

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        try:
            prompt_label = "\nchoice> " if terminal.needs_input else "\nhh> "
            user_input = input(prompt_label).strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/skills":
            print(agent.skill_store.render_index())
            continue
        if user_input.startswith("/skills "):
            try:
                print(agent.skill_store.read(user_input.split(maxsplit=1)[1]))
            except ValueError as exc:
                print(f"skill error: {exc}", file=sys.stderr)
            continue
        if user_input == "/model" or user_input == "/models":
            print(f"current: {_model_status(agent)}")
            print(f"available: {', '.join(PROVIDER_NAMES)}")
            print("switch: /model <provider> [model]")
            continue
        if user_input.startswith("/model "):
            parts = user_input.split(maxsplit=2)
            provider = parts[1]
            requested_model = parts[2] if len(parts) == 3 else None
            try:
                with agent_lock:
                    print(_switch_model(agent, args, provider, requested_model))
            except RuntimeError as exc:
                print(f"model switch failed: {exc}", file=sys.stderr)
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/tasks":
            print(agent.project_tasks.render())
            continue
        if user_input == "/crons":
            print(agent.cron_scheduler.render())
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            with agent_lock:
                # 处理本条请求；AgentLoop 会控制这次任务的生命周期。
                result = agent.ask(user_input)
                if not terminal.final_rendered:
                    print(result)
        except RuntimeError as exc:
            if not terminal.final_rendered:
                print(str(exc), file=sys.stderr)
        except KeyboardInterrupt:
            print("\nInterrupted. Run state and checkpoint were saved; the REPL is still available.")
