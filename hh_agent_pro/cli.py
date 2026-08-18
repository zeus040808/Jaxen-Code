"""命令行入口。

这个模块负责把“用户怎么启动 HH”翻译成 runtime 能理解的对象：
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
from .providers.clients import (
    AnthropicCompatibleModelClient,
    OllamaModelClient,
    OpenAIChatCompatibleModelClient,
    OpenAICompatibleModelClient,
)
from .providers.codex_cli import CodexCliModelClient
from .runtime import HH, SessionStore
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
WELCOME_SUBTITLE = "DeepSeek-default multi-model coding agent harness"
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


DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
DEFAULT_CODEX_TIMEOUT = 300
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
SECRET_ENV_NAMES_VAR = "HH_SECRET_ENV_NAMES"
PROVIDER_NAMES = ("codex", "deepseek", "ollama", "openai", "anthropic")

# 决定这次 agent 到底用哪个模型名
# args：命令行参数对象
# provider：当前使用哪个模型服务商
def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "codex":
        # Omit --model by default so Codex uses the authenticated local config.
        return "codex-config-default"
    if provider == "deepseek":
        model = provider_env("HH_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL", "LLM_MODEL_ID"))
        return model or DEFAULT_DEEPSEEK_MODEL
    # 选择模型，根据命令行参数，从.env中提取
    if provider == "openai":
        model = provider_env("HH_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = provider_env("HH_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    return DEFAULT_OLLAMA_MODEL

# 需要加密的环境变量名
DEFAULT_SECRET_ENV_NAMES = (
    "HH_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "HH_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "HH_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "HH_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
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
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "codex":
        client = CodexCliModelClient(
            workspace_root=args.cwd,
            executable=getattr(args, "codex_path", "codex"),
            model=getattr(args, "model", None),
            timeout=getattr(args, "codex_timeout", DEFAULT_CODEX_TIMEOUT),
        )
        client.provider = provider
        return client
    if provider == "deepseek":
        api_key = provider_env("HH_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        if not api_key:
            raise RuntimeError(
                "DeepSeek API key is missing. Set HH_DEEPSEEK_API_KEY or "
                "DEEPSEEK_API_KEY in the project .env file."
            )
        client = OpenAIChatCompatibleModelClient(
            model=_effective_model(args, provider),
            base_url=(
                getattr(args, "base_url", None)
                or provider_env(
                    "HH_DEEPSEEK_API_BASE",
                    ("DEEPSEEK_API_BASE", "BASE_URL"),
                    DEFAULT_DEEPSEEK_BASE_URL,
                )
            ),
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", 300),
        )
        client.provider = provider
        return client
    if provider == "openai":
        model = _effective_model(args, provider)
        # 先看命令行中有没有base_url
        base_url = getattr(args, "base_url", None) or provider_env("HH_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL)
        api_key = provider_env(
            "HH_OPENAI_API_KEY",
            ("OPENAI_API_KEY", "HH_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "HH_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        )
        client = OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
        client.provider = provider
        return client
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("HH_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), DEFAULT_ANTHROPIC_BASE_URL)
        api_key = provider_env(
            "HH_ANTHROPIC_API_KEY",
            ("ANTHROPIC_API_KEY", "HH_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "HH_OPENAI_API_KEY", "OPENAI_API_KEY"),
        )
        client = AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
        client.provider = provider
        return client
    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    client = OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
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
            agent.refresh_prefix(force=True)
        except Exception:
            agent.model_client = old_client
            agent.refresh_prefix(force=True)
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


def build_agent(args, event_sink=None):
    """根据 CLI 参数装配出一个可运行的 Agent 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `HH`，或一个从旧 session 恢复出来的 `HH`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    # 加载环境变量
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)

    # 创建 SessionStore对象 sessions 文件夹（目录）
    store = SessionStore(workspace.repo_root + "/.hh/sessions")

    # 构建大模型
    # Codex needs the resolved repository root, not the caller's possibly relative cwd.
    if getattr(args, "provider", "deepseek") == "codex":
        args.cwd = workspace.repo_root
    model = _build_model_client(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        # 调用from_session函数（在runtime中实现）
        return HH.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            event_sink=event_sink,
            feature_flags={"skill_learning": not bool(getattr(args, "no_auto_skills", False))},
        )
    return HH(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        event_sink=event_sink,
        feature_flags={"skill_learning": not bool(getattr(args, "no_auto_skills", False))},
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
    parser.add_argument("--provider", choices=PROVIDER_NAMES, default="deepseek", help="Model backend to use. DeepSeek V4 Pro is the default.")

    # --model 用来临时覆盖模型名
    parser.add_argument(
        "--model",
        default=None,
        help="Model override. For Codex, omit this to use the model in local Codex config.",
    )
    # 这个主要给 Ollama 用
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for deepseek, openai, or anthropic.")

    # 请求超时时间
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--codex-timeout", type=int, default=DEFAULT_CODEX_TIMEOUT, help="Codex CLI turn timeout in seconds.")
    parser.add_argument("--codex-path", default="codex", help="Codex CLI executable name or path.")

    #用来恢复之前的 session（默认新建session）
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    # 控制危险工具是否需要审批
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    # 哪些环境变量名应该被当作密钥，在 trace/report 里打码（110行使用）
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )

    parser.add_argument("--max-steps", type=int, default=12, help="Maximum effective tool calls per request (default: 12).")
    #限制模型每次最多输出多少 token
    parser.add_argument("--max-new-tokens", type=int, default=2048, help="Maximum model output tokens per step.")
    parser.add_argument("--no-auto-skills", action="store_true", help="Disable automatic SKILL.md deposition after verified complex runs.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    return parser


def main(argv=None):
    # 读取参数（把用户输入的命令行参数解析成一个对象）
    args = build_arg_parser().parse_args(argv)
    # 实现一个agent对象
    terminal = TerminalEventSink()
    agent = build_agent(args, event_sink=terminal)
    restored_crons = agent.cron_scheduler.start()

    # 上锁
    agent_lock = threading.Lock()

    model = _model_status(agent)
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
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
                    print("Codex requested user input, but stdin is not interactive.", file=sys.stderr)
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
                result = agent.ask(user_input)
                if not terminal.final_rendered:
                    print(result)
        except RuntimeError as exc:
            if not terminal.final_rendered:
                print(str(exc), file=sys.stderr)
        except KeyboardInterrupt:
            print("\nInterrupted. Run state and checkpoint were saved; the REPL is still available.")
