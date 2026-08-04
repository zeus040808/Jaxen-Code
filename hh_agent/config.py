"""
config.py — 全局配置：LLM 客户端、路径常量、系统提示词构造
"""
import os
import platform
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ── LLM 客户端 ───────────────────────────────────────
api_key = os.getenv("HH_DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise RuntimeError("未设置 DEEPSEEK_API_KEY，请检查 .env 文件")

client = OpenAI(
    api_key=api_key,
    base_url=os.getenv("HH_DEEPSEEK_API_BASE") or os.getenv("BASE_URL") or "https://api.deepseek.com",
)
MODEL = os.getenv("HH_DEEPSEEK_MODEL") or os.getenv("LLM_MODEL_ID") or "deepseek-chat"

# ── 路径常量 ─────────────────────────────────────────
WORKDIR = Path.cwd()
SKILLS_DIR = Path(__file__).parent / "skills"

# ── 操作系统提示（给 LLM 的工具选择建议）─────────────
OS_NAME = platform.system()
OS_HINT = (
    f"当前操作系统是 {OS_NAME}。查找/读取文件优先用 glob、read_file 工具而非 bash;"
    f"必须用 bash 时用{'Windows 命令 dir、type' if OS_NAME == 'Windows' else 'Unix 命令 ls、cat'}。"
    "所有操作限制在工作目录内。"
)

# 整个的system prompt构造器
def build_system(role: str, skills_summary: str = "",memory_index:str="") -> str:
    """构造系统提示词。skills_summary 为空时省略技能目录。"""
    parts = [role, OS_HINT]
    # 如果skills存在
    if skills_summary:
        parts.append(
            f"你拥有以下技能(仅简介),需要详情时调用 load_skill 加载:\n{skills_summary}"
        )
    if memory_index:
        parts.append(
            f"你记得以下长期信息(索引,相关内容会在需要时按需注入):\n{memory_index}"
        )
    parts.append("用中文回答。")
    return "\n".join(parts)


MAIN_ROLE = "你是编程助手,工作在本地代码仓库中。"
SUB_ROLE  = "你是子 Agent,直接用工具完成任务,不要再委派;完成后用一段中文总结结论。"

SYSTEM_PROMPT: str = ""      # 占位,由启动时装配填入
SUB_SYSTEM: str = ""



# ── 落盘目录(代码自动创建-compaction时使用) ──
PERSIST_DIR = WORKDIR / ".task_outputs" / "tool-results"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"

TOOL_RESULT_MAX_BYTES = 200_000
KEEP_RECENT_TOOL_RESULTS = 3
MICRO_PLACEHOLDER = "[较早的工具结果已压缩,需要时请重新调用对应工具]"

THRESHOLD = 48_000              # 估算 token 超过就 LLM 摘要,按你模型上下文窗口调
MAX_REACTIVE_RETRIES = 1

DEFAULT_MAX_TOKENS   = 4096
ESCALATED_MAX_TOKENS = 8192       # deepseek
MAX_RETRIES          = 10
MAX_RECOVERY_RETRIES = 3
FALLBACK_MODEL       = os.getenv("FALLBACK_MODEL_ID",None)   # 可选,没配就 None