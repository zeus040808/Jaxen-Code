"""子 agent 的运行与"信封"：调查（investigate）和审查（review）共用。

子 agent 是同步的：父 agent 停下来等它跑完。为了让父 agent 知道它花了多少、靠不靠谱，结果装进一个固定格式的信封
（参考 Claude Code 的 <task-notification>）：状态、摘要、结果、读过的文件、用量（token、请求、工具次数、耗时）。
结果太长时完整内容写进 .jaxen/subagents/，信封里只放摘要和文件路径，父 agent 的上下文保持精简；需要细节再 read_file。
"""

import time
import uuid
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

MAX_INLINE_RESULT_CHARS = 4_000
SUMMARY_CHARS = 300


@dataclass
class SubagentReport:
    kind: str
    status: str
    stop_reason: str
    answer: str
    files: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    output_file: str = ""


def run_child(agent, kind, task, max_steps):
    """同步运行一个只读子 agent，返回 (回答, 报告)。用量由子 agent 的事件出口累计（见 UsageForwardingSink）。"""
    started = time.monotonic()
    child = agent.readonly_child(max_steps)
    answer = child.ask(task)
    state = child.current_run_state
    evidence = child.source_evidence.to_dict().get("files", {})
    report = SubagentReport(
        kind=kind,
        status=state.status,
        stop_reason=str(state.stop_reason),
        answer=str(answer or ""),
        files=[{"path": item["path"], "summary": item["summary"]} for item in list(evidence.values())[:12]],
        usage={**child.event_sink.totals, "tool_uses": int(state.tool_steps),
               "duration_ms": int((time.monotonic() - started) * 1000)},
    )
    if len(report.answer) > MAX_INLINE_RESULT_CHARS:
        folder = agent.root / ".jaxen" / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{kind}-{uuid.uuid4().hex[:8]}.md"
        path.write_text(report.answer, encoding="utf-8")
        report.output_file = str(path.relative_to(agent.root))
    return report.answer, report


def envelope(report):
    """给父 agent 看的信封（XML 标签对模型友好，也和后台任务通知的格式一致）。"""
    result = report.answer
    if report.output_file:
        result = result[:MAX_INLINE_RESULT_CHARS] + f"\n...[truncated; full result in {report.output_file}]"
    summary = " ".join(report.answer.split())[:SUMMARY_CHARS]
    usage = report.usage
    lines = [
        "<subagent-result>",
        f"<kind>{report.kind}</kind>",
        f"<status>{escape(report.status)}</status>",
        f"<stop-reason>{escape(report.stop_reason)}</stop-reason>",
        f"<summary>{escape(summary)}</summary>",
        f"<result>{escape(result)}</result>",
    ]
    if report.output_file:
        lines.append(f"<output-file>{escape(report.output_file)}</output-file>")
    if report.files:
        lines.append("<files>")
        lines += [f"- {escape(item['path'])}: {escape(str(item['summary']))}" for item in report.files]
        lines.append("</files>")
    lines += [
        "<usage>",
        f"<input-tokens>{usage.get('input_tokens', 0)}</input-tokens>",
        f"<output-tokens>{usage.get('output_tokens', 0)}</output-tokens>",
        f"<requests>{usage.get('requests', 0)}</requests>",
        f"<tool-uses>{usage.get('tool_uses', 0)}</tool-uses>",
        f"<duration-ms>{usage.get('duration_ms', 0)}</duration-ms>",
        "</usage>",
        "</subagent-result>",
    ]
    return "\n".join(lines)
