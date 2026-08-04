# compaction.py — s08 上下文压缩
import json
import datetime
from .config import client, MODEL, WORKDIR, TOOL_RESULT_MAX_BYTES, KEEP_RECENT_TOOL_RESULTS, MICRO_PLACEHOLDER,PERSIST_DIR, TRANSCRIPT_DIR

# ── 消息形状判断(OpenAI 格式) ──
def _is_tool_msg(m):
    return m.get("role") == "tool"

# 判断是否为带 tool_calls 的 assistant 消息（模型要求调工具）
def _has_tool_calls(m):
    return m.get("role") == "assistant" and bool(m.get("tool_calls"))

# tag = tool_call_id
def persist_large_output(tag: str, content: str) -> str:
    """把超大 tool_result 写到磁盘,上下文里只留标记+预览(可恢复)。"""
    #创建文件夹
    PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    #生成文件名
    fname = f"{tag}.txt"
    (PERSIST_DIR / fname).write_text(content, encoding="utf-8")
    #生成相对路径
    rel = f".task_outputs/tool-results/{fname}"
    #保留前2000字符
    preview = content[:2000]
    return (f"<persisted-output path=\"{rel}\">\n{preview}\n"
            f"...[完整内容已落盘,共 {len(content)} 字符,需要时用 read_file 读取 {rel}]\n"
            f"</persisted-output>")

def write_transcript(messages: list):
    """压缩前先把完整对话存档为 JSONL(可恢复记录)。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = TRANSCRIPT_DIR / f"transcript-{ts}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m, ensure_ascii=False, default=str) + "\n")
    return path

def estimate_token_count(messages: list) -> int:
    """粗估:约 3 字符/token,只用来决定要不要触发摘要。"""
    return len(json.dumps(messages, ensure_ascii=False, default=str)) // 3
# 捕获 413/上下文超长

# ═══ L3: 大结果落盘(必须最先跑) ═══
# 将大tool_result按从大到小的顺序放到硬盘里，直到总的结果小于目标
def tool_result_budget(messages, max_bytes=TOOL_RESULT_MAX_BYTES):
    #工具调用message
    tool_msgs = [m for m in messages if _is_tool_msg(m)]
    total = sum(len(str(m.get("content", ""))) for m in tool_msgs)
    if total <= max_bytes:
        return messages
    #从大到小开始处理tool_result
    for m in sorted(tool_msgs, key=lambda x: len(str(x.get("content", ""))), reverse=True):
        #预算够了就停止
        if total <= max_bytes:
            break
        c = str(m.get("content", ""))
        if c.startswith("<persisted-output"):        # 已落盘的跳过
            continue
        m["content"] = persist_large_output(m.get("tool_call_id", "unknown"), c)
        total = sum(len(str(x.get("content", ""))) for x in tool_msgs)
    return messages

# ═══ L1: 裁掉对话中间(保头保尾,不拆散 tool_calls ↔ tool 组) ═══
# 裁掉所有类型的message
def snip_compact(messages, max_messages=50):
    if len(messages) <= max_messages:
        return messages
    head_end = 3
    tail_start = len(messages) - (max_messages - 3)
    # 头部:若 head 最后一条是带 tool_calls 的 assistant,把它的 tool 应答也包进 head
    if head_end - 1 >= 0 and _has_tool_calls(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_msg(messages[head_end]):
            head_end += 1
    # 尾部:若 tail 第一条是 tool 消息,往前退到包含它的 assistant,避免孤立
    while tail_start > head_end and _is_tool_msg(messages[tail_start]):
        tail_start -= 1
    if tail_start <= head_end:
        return messages
    snipped = tail_start - head_end
    placeholder = {"role": "user", "content": f"[已裁掉对话中间 {snipped} 条无关的旧消息]"}
    #在这里用head_end和tail_start重新确定messages列表范围和内容
    return messages[:head_end] + [placeholder] + messages[tail_start:]

# ═══ L2: 旧 tool_result 占位(保留最近 3 条) ═══
def micro_compact(messages):
    tool_msgs = [m for m in messages if _is_tool_msg(m)]
    if len(tool_msgs) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    for m in tool_msgs[:-KEEP_RECENT_TOOL_RESULTS]:
        c = str(m.get("content", ""))
        if len(c) > 120 and c != MICRO_PLACEHOLDER and not c.startswith("<persisted-output"):
            m["content"] = MICRO_PLACEHOLDER
    return messages

# ═══ 预处理管线:顺序固定 L3 → L1 → L2(0 API) ═══
def preprocess_context(messages):
    messages[:] = tool_result_budget(messages)    # L3: 大结果落盘
    messages[:] = snip_compact(messages)          # L1: 裁中间
    messages[:] = micro_compact(messages)         # L2: 旧结果占位
    return messages

# ═══ L4: LLM 全量摘要(1 API) ═══
def summarize_history(messages) -> str:
    convo = "\n".join(f"{m.get('role')}: {str(m.get('content'))[:2000]}"
                      for m in messages if m.get("role") != "system")
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content":
             "你是对话压缩器。把下面的 agent 工作历史压成一段简洁摘要,必须保留:"
             "当前目标、关键发现、已创建/修改的文件、剩余待办、用户给的约束。"},
            {"role": "user", "content": convo[:60000]},
        ],
    )
    return resp.choices[0].message.content

def compact_history(messages):
    write_transcript(messages)                    # 先存档
    # 用LLM生成摘要
    summary = summarize_history(messages)
    system_msg = messages[0]
    tail_start = max(1, len(messages) - 5)  # 保留 system + 最后 3 条
    while tail_start > 1 and _is_tool_msg(messages[tail_start]):
        tail_start -= 1
    # 保留 system(技能目录/OS提示别丢!)
    return [system_msg,
            {"role": "user", "content": f"[历史已压缩]\n\n{summary}"},
            *messages[tail_start:]]

# ═══ 应急:reactive_compact(API 仍报 413 时) ═══
def reactive_compact(messages):
    write_transcript(messages)
    summary = summarize_history(messages)
    system_msg = messages[0]
    tail_start = max(1, len(messages) - 5)         # 保留 system + 最后 5 条
    while tail_start > 1 and _is_tool_msg(messages[tail_start]):
        tail_start -= 1                            # 别留孤立 tool 消息
    return [system_msg,
            {"role": "user", "content": f"[应急压缩]\n\n{summary}"},
            *messages[tail_start:]]