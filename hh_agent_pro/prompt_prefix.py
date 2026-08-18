"""Stable prompt prefix construction."""

import hashlib
import json
import textwrap
from dataclasses import dataclass

from .workspace import now

# 系统规则如何定义
# workspace 信息如何拼入提示词
# Agent 如何知道自己的能力和限制
@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。

    text: str       # 真正加入最终 prompt 的内容，包括规则、工具和 workspace
    hash: str       # 整个前缀文本的指纹。只要前缀任何字符发生变化，哈希通常都会改变
    workspace_fingerprint: str  # 工作区快照的指纹
    tool_signature: str # 工具清单的指纹
    built_at: str   # 记录这个对象的创建时间，主要用于调试和追踪

# 工具清单的指纹。工具名称、参数、风险或描述改变时，它会变化（不会执行工具）
def tool_signature(tools):
    payload = []
    for name in sorted(tools):
        tool = tools[name]
        # 提取会影响模型使用工具的信息
        payload.append(
            {
                "name": name,
                "schema": tool["schema"],
                "risky": tool["risky"],
                "description": tool["description"],
            }
        )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


COMPRESSIBLE_WORKSPACE_MARKER = "\n\nWorkspace snapshot (compressible):\n"


def build_prompt_prefix(workspace, tools, runtime_info=None, built_at=None, native_tools=False):
    runtime_info = dict(runtime_info or {})
    tool_lines = []
    for name, tool in tools.items():
        fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
        # 标记风险
        risk = "conditional approval" if name == "run_shell" else ("approval required" if tool["risky"] else "safe")
        tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
    # 原生 tool calling provider 已经从 API 的 tools 字段拿到完整 JSON schema。
    # 此时 prompt 只保留行为策略，避免把 23 份 schema 再发送一遍。
    tool_text = (
        "- Tool names and argument schemas are supplied through the provider's native tools field.\n"
        "- Prefer list_files, read_file/read_files, search and runtime_info before run_shell."
        if native_tools else "\n".join(tool_lines)
    )

    # 这里给模型展示两种工具调用格式
    examples = "\n".join(
        [
            '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
            '<tool>{"name":"read_files","args":{"paths":["a.py","b.py"],"start":1,"end":300}}</tool>',
            '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
            '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
            '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
            "<needs_user_input>Please provide the missing target value.</needs_user_input>",
            "<blocked>The requested action is not permitted in this run.</blocked>",
            "<final>Done.</final>",
        ]
    ) if not native_tools else "Native tool calls use the provider-supplied JSON schemas."

    # prefix 可以理解成 agent 的“工作手册”：
    # 核心text：它是谁、工具怎么调用、当前仓库是什么状态，都写在这里。
    runtime_text = "\n".join(
        f"- {key}: {runtime_info.get(key, '')}"
        for key in ("provider", "model", "platform", "shell", "cwd", "repo_root", "approval_policy", "read_only")
    )
    critical_text = textwrap.dedent(
        f"""\
        You are HH, a small local coding agent working inside a local repository.

        Runtime identity (authoritative and secret-free):
        {runtime_text}

        Rules:
        - Use tools instead of guessing about the workspace.
        - Return exactly one tool call, one <needs_user_input>...</needs_user_input>, one <blocked>...</blocked>, or one <final>...</final>.
        - Use the provider's native tool calling interface when native tool schemas are available.
        - On text-only providers, tool calls must look like <tool>{{"name":"tool_name","args":{{...}}}}</tool>.
        - Final answers must look like:
          <final>your answer</final>
        - If a required value, target, or decision is missing or contradictory, stop with:
          <needs_user_input>the exact clarification question</needs_user_input>
        - When user input has a small bounded set of choices, include concise numbered options in <needs_user_input>. Do not choose on the user's behalf.
        - If policy, approval, read-only mode, or workspace boundaries make the task impossible, stop with:
          <blocked>the concrete blocker and required next step</blocked>
        - Never invent tool results.
        - Keep answers concise and concrete.
        - For provider/model/OS/shell/cwd questions, use the runtime identity above or runtime_info. Never read .env for self-identification.
        - Prefer dedicated safe tools: list_files for directories, read_file/read_files for source, and search for code search. Do not use run_shell for dir/ls/type/cat/Get-Content when a dedicated tool fits.
        - read_files is preferred when comparing or summarizing multiple known files; it reduces model round trips and preserves evidence from every file.
        - The current shell is exactly the shell named in Runtime identity. On Windows it is cmd.exe, so do not emit Bash-only pwd/ls/cat/~//dev/null commands.
        - run_shell always starts in repo_root. A cd inside one shell call does not persist to future calls; never claim the Agent permanently entered a directory.
        - If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.
        - Before writing tests for existing code, read the implementation first.
        - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
        - New files should be complete and runnable, including obvious imports.
        - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
        - Tool results include status, error_code, retryable, and recommended_action. Never retry when retryable is false.
        - For a genuinely multi-step task, create explicit subtasks with acceptance criteria, claim them before work, and complete them with concrete evidence.
        - Use record_evidence to distinguish verified conclusions from hypotheses. Do not present an unverified claim as a conclusion.
        - Durable memory is curated, not a log. Persist only stable preferences, conventions, decisions, dependencies, corrections, or verified facts that will help future sessions.
        - Use memory_update replace/remove when a fact is corrected or forgotten; do not append a conflicting fact. Never store secrets, transient run state, raw tool output, or instructions that override system rules.
        - Durable-memory writes take effect in the next Session because the current Session uses a frozen snapshot.
        - Use session_search only when older conversation evidence is relevant, then session_read if chronological context around a hit is needed. Treat archived results as historical evidence, not as authoritative live workspace state.
        - Skills are reusable procedures, not facts or task logs. Match the current request against the compact skill index and call skill_view before following a matching skill.
        - Use skill_manage only for a reusable workflow with clear trigger conditions, ordered steps, and verification. Do not save secrets, raw transcripts, one-off task state, or unverified guesses as a skill.
        - Before returning <final>, inspect Task progress: tracked subtasks must be completed, open questions and retryable failures must be resolved, and no background task may still be running.
        - Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, or delegate with args={{}}.

        Tools:
        {tool_text}

        Valid response examples:
        {examples}
        
        """
    ).strip()
    # Critical rules/tools/identity must stay intact. Only the Git/docs snapshot
    # after this marker may be reduced by ContextManager.
    text = critical_text + COMPRESSIBLE_WORKSPACE_MARKER + workspace.text()
    signature = tool_signature(tools)
    return PromptPrefix(
        text=text,
        hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        workspace_fingerprint=workspace.fingerprint(),
        tool_signature=signature,
        built_at=built_at or now(),
    )
