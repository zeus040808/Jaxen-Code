"""Session JSON persistence."""

import json
import tempfile
import time
from pathlib import Path

SESSION_REPLACE_ATTEMPTS = 5

# 让 Jaxen 在多轮对话、重启程序、恢复会话后，仍然知道之前发生过什么
class SessionStore:
    def __init__(self, root):
        # session文件夹
        self.root = Path(root)   # 把字符串路径转成 Path 对象，方便后续用 / 拼接文件名
        self.root.mkdir(parents=True, exist_ok=True)

    # 把 session ID 转成文件路径
    def path(self, session_id):
        session_id = str(session_id).strip()
        if not session_id or session_id in {".", ".."} or "/" in session_id or "\\" in session_id:
            raise ValueError("invalid session id")
        return self.root / f"{session_id}.json"   # 输入一个 session_id（如 "abc123"），输出 /root目录/abc123.json

    # 把内存中的 Python 字典写成格式化 JSON（根据session id保存）
    def save(self, session):
        if not isinstance(session, dict) or not session.get("id"):
            raise ValueError("session must be a dictionary with a non-empty id")
        path = self.path(session["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
            ) as handle:
                json.dump(session, handle, indent=2, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                temp_name = handle.name
            temp_path = Path(temp_name)
            for attempt in range(SESSION_REPLACE_ATTEMPTS):
                try:
                    temp_path.replace(path)
                    break
                except PermissionError:
                    if attempt == SESSION_REPLACE_ATTEMPTS - 1:
                        raise
                    # OneDrive and antivirus scanners can briefly hold the
                    # destination between the temp write and atomic replace.
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
        return path

    # 读取 JSON，恢复为 Python 字典
    def load(self, session_id):
        session = json.loads(self.path(session_id).read_text(encoding="utf-8"))
        if not isinstance(session, dict):
            raise ValueError("session file must contain a JSON object")
        if str(session.get("id", "")) != str(session_id):
            raise ValueError("session id does not match the session filename")
        return session

    # 按文件最后修改时间排序，返回最近更新的 session ID
    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
