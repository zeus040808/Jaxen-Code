"""Jaxen 的本地 UI 服务：浏览器界面 ↔ 本服务 ↔ `python -m hh_agent_pro --jsonl`。

UI 不碰 agent 内部：每轮对话启动一次 CLI 的 JSONL 机器协议（评测用的同一个接口），
把它逐行输出的事件原样转发给浏览器。会话靠协议里的 sessionId 续上，审批靠 responses 回传。
所以 CLI、UI、评测共用同一个 agent 核心，UI 只是另一种"前端"。

    python -m hh_agent_ui --cwd <要操作的仓库> [--port 8765]
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HH_ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
DIST = STATIC / "dist"  # web/ 里 npm run build 的产物（AI Elements 版界面）；没有就用 static/index.html
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".wasm": "application/wasm", ".json": "application/json", ".png": "image/png"}
MAX_DIFF_BYTES = 200_000
APPROVAL_MODES = ("auto", "ask", "never")
MAX_UPLOAD_BYTES = 10_000_000
MAX_LISTED_FILES = 200
SKIP_DIRS = {".git", ".hh", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache"}


def build_command(python, workspace, approval, model=""):
    if approval not in APPROVAL_MODES:
        raise ValueError(f"approval must be one of {APPROVAL_MODES}")
    command = [python, "-m", "hh_agent_pro", "--jsonl", "--cwd", str(workspace), "--approval", approval]
    if model:
        command += ["--model", model]
    return command


def request_line(text, session_id=None, responses=None):
    """一行 JSONL 请求，格式见 hh_agent_pro/jsonl_protocol.py::validate_request。"""
    return json.dumps({
        "protocolVersion": 1,
        "requestId": f"ui-{uuid.uuid4().hex[:12]}",
        "text": text,
        "sessionId": session_id or None,
        "responses": responses or [],
    }, ensure_ascii=False) + "\n"


def list_dirs(raw):
    """文件夹选择器：列出某个目录下的子目录（不读文件内容）。浏览器拿不到本地绝对路径，只能由本服务来列。"""
    path = Path(raw or Path.home()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    dirs = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            dirs.append({"name": entry.name, "isRepo": (Path(entry.path) / ".git").exists()})
    except PermissionError:
        pass  # macOS 的受保护目录：显示为空，不报错
    return {"path": str(path), "parent": str(path.parent) if path.parent != path else None,
            "isRepo": (path / ".git").exists(), "dirs": dirs[:500]}


def list_files(workspace, query=""):
    """附加文件时用的文件列表：git 仓库用 git ls-files（遵守 .gitignore），否则遍历并跳过依赖目录。"""
    listed = subprocess.run(["git", "-C", str(workspace), "ls-files", "-co", "--exclude-standard"],
                            capture_output=True, text=True)
    if listed.returncode == 0:
        names = [n for n in listed.stdout.splitlines() if n and not n.startswith(".hh/")]
    else:
        names = []
        for root, dirnames, filenames in os.walk(workspace):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            names += [str(Path(root, f).relative_to(workspace)) for f in filenames]
            if len(names) > 20_000:
                break
    query = query.lower().strip()
    matches = [n for n in sorted(names) if query in n.lower()] if query else sorted(names)
    return {"files": matches[:MAX_LISTED_FILES], "total": len(matches)}


def save_upload(workspace, name, data):
    """把从电脑上传的文件存进 <工作区>/.hh/uploads/，返回相对路径，agent 用 read_file 就能读。"""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file too large (> {MAX_UPLOAD_BYTES // 1_000_000} MB)")
    safe = re.sub(r"[^\w.\-]+", "_", Path(str(name)).name).strip("._") or "upload"  # 只留文件名，防 ../
    folder = workspace / ".hh" / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target, stem, suffix, n = folder / safe, Path(safe).stem, Path(safe).suffix, 1
    while target.exists():  # 同名不覆盖
        target, n = folder / f"{stem}-{n}{suffix}", n + 1
    target.write_bytes(data)
    return str(target.relative_to(workspace))


def git(workspace, *args):
    result = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


class Handler(BaseHTTPRequestHandler):
    server_version = "HHAgentUI/0.1"

    def log_message(self, fmt, *args):  # 安静一点：只记错误
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _workspace(self, raw):
        path = Path(raw or self.server.default_workspace).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"workspace not found: {path}")
        return path

    def do_GET(self):
        url = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                self._file((DIST if (DIST / "index.html").exists() else STATIC) / "index.html")
            elif url.path.startswith("/assets/"):
                target = (DIST / url.path.lstrip("/")).resolve()
                if DIST.resolve() not in target.parents or not target.is_file():  # 不允许 ../ 越出 dist
                    return self._json({"error": "not found"}, 404)
                self._file(target)
            elif url.path == "/api/info":
                workspace = self._workspace(query.get("cwd"))
                self._json({
                    "workspace": str(workspace),
                    "branch": git(workspace, "rev-parse", "--abbrev-ref", "HEAD").strip(),
                    "defaultWorkspace": str(self.server.default_workspace),
                })
            elif url.path == "/api/fs":
                self._json(list_dirs(query.get("path")))
            elif url.path == "/api/files":
                self._json(list_files(self._workspace(query.get("cwd")), query.get("q", "")))
            elif url.path == "/api/diff":
                workspace = self._workspace(query.get("cwd"))
                diff = git(workspace, "diff", "--no-color", "HEAD")[:MAX_DIFF_BYTES]
                # .hh/ 是 agent 自己的会话与 trace 目录，不算用户代码的改动。
                untracked = [line[3:] for line in git(workspace, "status", "--porcelain").splitlines()
                             if line.startswith("??") and not line[3:].startswith(".hh")]
                self._json({"diff": diff, "untracked": untracked[:200]})
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/turn":
                return self._turn(payload)
            if url.path == "/api/upload":
                workspace = self._workspace(payload.get("cwd"))
                data = base64.b64decode(str(payload.get("data", "")), validate=True)
                return self._json({"path": save_upload(workspace, payload.get("name", ""), data)})
            if url.path == "/api/cancel":
                process = self.server.running.pop(str(payload.get("turnId", "")), None)
                if process and process.poll() is None:
                    process.terminate()
                return self._json({"cancelled": bool(process)})
            self._json({"error": "not found"}, 404)
        except (ValueError, json.JSONDecodeError, base64.binascii.Error) as exc:
            self._json({"error": str(exc)}, 400)

    def _turn(self, payload):
        """启动一轮 agent，把它的 JSONL 输出逐行流给浏览器（NDJSON）。"""
        workspace = self._workspace(payload.get("cwd"))
        command = build_command(self.server.python, workspace, payload.get("approval", "auto"),
                                str(payload.get("model", "")).strip())
        process = subprocess.Popen(
            command, cwd=HH_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        turn_id = uuid.uuid4().hex[:12]
        self.server.running[turn_id] = process
        stderr_tail = []
        threading.Thread(target=lambda: stderr_tail.extend(process.stderr.readlines()[-20:]), daemon=True).start()
        process.stdin.write(request_line(payload.get("text", ""), payload.get("sessionId"), payload.get("responses")))
        process.stdin.close()

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        saw_turn = False
        try:
            self.wfile.write((json.dumps({"kind": "ui", "turnId": turn_id}) + "\n").encode())
            self.wfile.flush()
            for line in process.stdout:
                if not line.strip():
                    continue
                saw_turn = saw_turn or '"kind": "turn"' in line
                self.wfile.write(line.encode() if line.endswith("\n") else (line + "\n").encode())
                self.wfile.flush()
            process.wait()
            if not saw_turn:  # 进程异常退出：把 stderr 末尾交给界面，别让用户干等
                tail = "".join(stderr_tail)[-1500:] or f"agent exited with code {process.returncode}"
                for row in ({"kind": "event", "type": "error", "errorType": "process_error", "message": tail},
                            {"kind": "turn", "status": "failed", "finalAnswer": "", "sessionId": payload.get("sessionId")}):
                    self.wfile.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            process.terminate()  # 浏览器关了或点了停止
        finally:
            self.server.running.pop(turn_id, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cwd", default=".", help="默认操作的仓库（界面里可以改）")
    parser.add_argument("--host", default="127.0.0.1", help="只监听本机；不要暴露到局域网")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--python", default=sys.executable, help="运行 hh_agent_pro 的解释器")
    args = parser.parse_args()

    from hh_agent_pro.config import load_project_env

    # 子进程从这里继承模型密钥：被操作的仓库里通常没有 .env。只读进环境变量，不打印、不落盘。
    load_project_env(HH_ROOT, override=False)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.default_workspace = Path(args.cwd).expanduser().resolve()
    server.python = args.python
    server.running = {}
    print(f"Jaxen UI: http://{args.host}:{args.port}  (workspace: {server.default_workspace})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
