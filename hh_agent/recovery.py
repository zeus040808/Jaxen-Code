"""recovery.py — s11 错误恢复:瞬态重试、过载降级、上下文超限判断"""
import time, random
from dataclasses import dataclass, field
from openai import (RateLimitError, InternalServerError,
                    APIConnectionError, APITimeoutError, BadRequestError)
from . import config

@dataclass
class RecoveryState:
    #当前用的模型。默认 config.MODEL，连续过载后可能被换成备用模型
    current_model: str = field(default_factory=lambda: config.MODEL)
    max_tokens: int = field(default_factory=lambda: config.DEFAULT_MAX_TOKENS)
    #是否已升级过 max_tokens（只给一次机会的闸门）
    has_escalated: bool = False
    #续写了几次（上限 MAX_RECOVERY_RETRIES）
    recovery_count: int = 0
    #是否已应急压缩过（只一次）
    has_attempted_reactive_compact: bool = False
    #连续过载计数（攒够 3 次切备用模型）
    consecutive_overload: int = 0

# 一个元组，列出「瞬态、等一会儿再试可能就好」的异常——429 限流、5xx（含过载）、连不上、超时。
TRANSIENT = (RateLimitError, InternalServerError, APIConnectionError, APITimeoutError)

# 尝试从异常里读 Retry-After 响应头——服务器有时会明确告诉你「等 X 秒再来」。
# 读到就转成 float 秒数，读不到/出错就返回 None。
def _retry_after(e):
    try:
        v = e.response.headers.get("retry-after")
        return float(v) if v else None
    except Exception:
        return None

def retry_delay(attempt, retry_after=None):
    # 先判断响应中有没有retry_after
    if retry_after:
        return retry_after
    base = min(500 * (2 ** attempt), 32000) / 1000          # 秒,封顶 32s
    return base + random.uniform(0, base * 0.25)          # +0~25% 抖动防惊群

# 重试包装器
def with_retry(fn, state, max_retries=None):
    max_retries = max_retries or config.MAX_RETRIES
    for attempt in range(max_retries):
        try:
            result = fn()
            state.consecutive_overload = 0                   # 成功就清零过载streak
            return result
        except TRANSIENT as e:
            if isinstance(e, InternalServerError):           # 5xx/过载才算
                state.consecutive_overload += 1
                if state.consecutive_overload >= 3 and config.FALLBACK_MODEL:
                    print(f"\033[33m[recovery] 连续过载,切备用模型 {config.FALLBACK_MODEL}\033[0m")
                    state.current_model = config.FALLBACK_MODEL
            delay = retry_delay(attempt, _retry_after(e))
            print(f"\033[33m[recovery] {type(e).__name__} 第{attempt+1}次,等 {delay:.1f}s\033[0m")
            time.sleep(delay)
    raise RuntimeError("重试次数已用尽")

# 判断一个异常是不是「上下文太长」
def is_context_overflow(e):
    # 只匹配"上下文"相关词,避开"max_tokens 太大"那种参数错(否则会误判)
    return isinstance(e, BadRequestError) and any(
        k in str(e).lower() for k in ("context", "context_length", "too long"))