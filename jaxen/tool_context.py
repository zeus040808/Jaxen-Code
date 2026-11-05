"""传给工具实现的受限上下文。

CodingAgent 只暴露工具需要的路径、环境和服务，避免工具依赖整个 Agent。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# 工具需要的受限运行环境
# 可以理解为工具的“依赖包
@dataclass
class ToolContext:
    root: Path
    path_resolver: Callable[[str], Path]
    shell_env_provider: Callable[[], dict]
    runtime_info_provider: Callable[[], dict]
    depth: int
    max_depth: int
    spawn_investigation: Callable[[dict], str]
    backlog: Any = None
    progress: Any = None
    background_jobs: Any = None
    long_term_memory: Any = None
    session_archive: Any = None
    skill_store: Any = None
    session_id: str = ""
    toolset: str = "full"

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()

    def runtime_info(self):
        return dict(self.runtime_info_provider())
