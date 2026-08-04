"""
loop.py — 核心 agent loop

流程：拼 system → 压缩 → 调 LLM(带恢复) → 有 tool_call?
  ├─ 是 → 权限检查 → 执行工具 → 结果追加 → 再调 LLM(循环)
  └─ 否 → 提取记忆 → 触发 Stop hook → 返回 messages

错误恢复(s11)三条路径:
  - 截断(finish_reason=="length") → 升级 max_tokens / 续写
  - 上下文超限(BadRequestError) → reactive_compact 一次
  - 瞬态(429/5xx/超时) → with_retry 内部指数退避+抖动,连续过载切备用模型
"""
import json

from . import config, skills_loader
from .compaction import (preprocess_context, compact_history,
                         reactive_compact, estimate_token_count)
from .security import trigger_hooks
from .tools import NAG_THRESHOLD, TOOLS, TOOL_HANDLERS
from .memory import load_memories, extract_memories, consolidate_memories, read_memory_index
from .recovery import RecoveryState, with_retry, is_context_overflow
from .background_tasks import (collect_background_results,
                               should_run_background, start_background_task)
from .cron_scheduler import consume_cron_queue

CONTINUATION_PROMPT = "输出被截断了。直接从刚才中断处继续,不要道歉、不要重述。"


# ═══════════════ system prompt 装配 + 缓存(s10) ═══════════════

# 调用build_system拼成完整的system_prompt
def build_main_system() -> str:
    """主 Agent 系统提示:角色 + OS + 技能目录 + 记忆索引(按真实状态拼)。"""
    return config.build_system(config.MAIN_ROLE,
                               skills_loader.list_skills(),
                               read_memory_index())

def build_sub_system() -> str:
    """子 Agent 系统提示:不带记忆索引。"""
    return config.build_system(config.SUB_ROLE, skills_loader.list_skills())

_sys_key = None
_sys_val = None

# 把工具列表、技能列表、工作目录、记忆索引四个"真实状态"做一次快照序列化成 JSON 字符串，
# 作为缓存 key。只有这些状态变了才会重新拼 system
def _context_snapshot() -> dict:
    """system prompt 依赖的真实状态——只有这些变了才需要重拼。"""
    return {
        "tools": sorted(TOOL_HANDLERS.keys()),
        "skills": skills_loader.list_skills(),
        "workspace": str(config.WORKDIR),
        "memory_index": read_memory_index(),
    }

def get_main_system() -> str:
    """带缓存的装配:状态没变直接返回上次结果,变了才重拼(替代手动刷新)。"""
    global _sys_key, _sys_val
    key = json.dumps(_context_snapshot(), sort_keys=True, ensure_ascii=False, default=str)
    if key == _sys_key and _sys_val is not None:
        return _sys_val
    _sys_key, _sys_val = key, build_main_system()
    return _sys_val


def _execute_background_tool(name: str, args: dict) -> str:
    """Execute one tool in a worker thread and preserve PostToolUse semantics."""
    handler = TOOL_HANDLERS[name]
    result = handler(**args)
    trigger_hooks("PostToolUse", name, args, result)
    return result


# ═══════════════ 核心循环 ═══════════════

def agent_loop(messages: list) -> list:
    """执行一轮 agent 循环:反复调 LLM 直到不再需要工具为止。"""
    state = RecoveryState()        # s11: 本轮恢复账本(每个用户轮重置,救援预算满格)
    rounds_since_todo = 0
    compact_failures = 0
    # s09: 轮首注入相关记忆(只一次,不进 while) 主要是选择相关记忆
    load_memories(messages)

    while True:
        # S14: 调度线程只负责入队；主循环消费后才把工作交给 LLM。
        # 它会把当前队列里的任务全部取出来，然后清空队列
        for job in consume_cron_queue():
            messages.append({"role": "user", "content": f"[Scheduled] {job.prompt}"})

        # S13: 后台完成事件是新的用户消息，不能复用已经应答过的 tool_call_id。
        notifications = collect_background_results()
        if notifications:
            messages.append({"role": "user", "content": "\n".join(notifications)})

        # ── s10: 按真实状态刷新 system(缓存,没变=免费空操作) ──
        messages[0]["content"] = get_main_system()

        # ── s09: 压缩前快照(给轮尾提取记忆用) ──
        pre_compress = list(messages)

        # ── s08: 三个 0-API 预处理器(L3落盘 → L1裁中间 → L2占位) ──
        preprocess_context(messages)
        # 预处理还不够 → LLM 全量摘要(熔断:连续失败 3 次停手)
        if estimate_token_count(messages) > config.THRESHOLD and len(messages) > 4 and compact_failures < 3:
            try:
                messages[:] = compact_history(messages)
                compact_failures = 0
                print("\033[90m[compact] 历史已 LLM 摘要压缩\033[0m")
            except Exception as e:
                compact_failures += 1
                print(f"\033[31m[compact 失败 {compact_failures}/3] {e}\033[0m")

        # ── s05: 连续 N 轮没调 todo_write → 注入提醒 ──
        if rounds_since_todo >= NAG_THRESHOLD and messages:
            messages.append({"role": "user",
                             "content": "<reminder>记得用 todo_write 更新你的任务进度。</reminder>"})
            rounds_since_todo = 0

        # ── s11: 调 LLM,with_retry 内部处理瞬态错误(429/5xx/超时) ──
        try:
            # 大模型回答生成
            response = with_retry(
                lambda: config.client.chat.completions.create(
                    model=state.current_model,      # 可能已切到备用模型
                    messages=messages,
                    tools=TOOLS,
                    max_tokens=state.max_tokens,     # 截断后可能已升级
                ),
                state,
            )
        except Exception as e:
            # 路径2:上下文超限 → 应急压缩一次再试
            if is_context_overflow(e) and not state.has_attempted_reactive_compact:
                print("\033[31m[recovery] 上下文超限,触发 reactive_compact\033[0m")
                messages[:] = reactive_compact(messages)
                state.has_attempted_reactive_compact = True
                continue
            # 其余无法恢复(密钥错/重试耗尽/压过还超) → 记录并优雅退出
            print(f"\033[31m[fatal] 无法恢复,退出本轮: {e}\033[0m")
            return messages

        msg = response.choices[0].message
        finish = response.choices[0].finish_reason

        # ── s11 路径1:输出被截断 ──
        if finish == "length":
            if not state.has_escalated:
                # 第一次:加大输出上限,重发"干净"请求(不把半截输出追加进 messages)
                state.max_tokens = config.ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print("\033[33m[recovery] 输出截断,升级 max_tokens 重试\033[0m")
                continue
            # 升级后还截断:保存半截输出 + 让它接着说,最多 MAX_RECOVERY_RETRIES 次
            messages.append({"role": "assistant", "content": msg.content or ""})
            if state.recovery_count < config.MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"\033[33m[recovery] 续写第 {state.recovery_count} 次\033[0m")
                continue
            print("\033[31m[recovery] 续写到上限仍截断,退出\033[0m")
            return messages

        # ── 正常:模型不再调工具 → 收尾 ──
        if not msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content})
            force = trigger_hooks("Stop", messages)
            # 为None继续下一步
            if force:
                messages.append({"role": "user", "content": force})
                continue
            extract_memories(pre_compress)   # s09 ②:从压缩前快照提取新记忆
            consolidate_memories()           # s09 ③:够阈值才整理去重
            return messages

        # ── 模型要调工具:先把 assistant(含 tool_calls) 追加进去 ──
        messages.append({
            "role": "assistant", "content": msg.content,
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls],
        })

        called_todo = False
        want_compact = False
        for tc in msg.tool_calls:
            name = tc.function.name
            if name == "todo_write":
                called_todo = True
            try:
                # 函数参数
                args = json.loads(tc.function.arguments)
                if name == "compact":                    # 可选:模型主动压缩
                    result = "[已压缩,历史已总结]"
                    want_compact = True
                else:
                    blocked = trigger_hooks("PreToolUse", name, args)   # s04 权限
                    # 验证是否超出权限范围
                    if blocked:
                        result = f"权限被拒绝: {blocked}"
                    #判断是否需要启动后台任务
                    elif should_run_background(name, args):
                        command = args.get("command", "")
                        background_args = {**args, "run_in_background": False}
                        background_id = start_background_task(
                            command,
                            # n = 当前的 name
                            # a = 当前的 background_args
                            # 之后外面的变量即使改变，lambda 仍然使用已经保存的值
                            lambda n=name, a=background_args: _execute_background_tool(n, a),
                        )
                        result = (f"后台任务 {background_id} 已启动，主循环无需等待；"
                                  "完成结果会通过 task_notification 返回。")
                    else:
                        handler = TOOL_HANDLERS.get(name)
                        result = handler(**args) if handler else f"未知工具: {name}"
                        trigger_hooks("PostToolUse", name, args, result)
            except Exception as e:
                result = f"工具执行出错:{e}"
            print(f"\033[33m> {name}\033[0m {str(result)[:200]}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

        # s05: 本轮调了 todo_write 就清零计数,否则累加
        rounds_since_todo = 0 if called_todo else rounds_since_todo + 1

        # 可选:模型主动 compact,turn 末统一执行
        if want_compact:
            messages[:] = compact_history(messages)
