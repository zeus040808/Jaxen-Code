"""UI 服务（jaxen.ui）：只通过 CLI 的 JSONL 协议驱动 agent，自己不碰 agent 内部。

用假的 agent 进程（读一行请求、吐几行事件）测流式转发，不调用真实模型。
"""

import json
import subprocess
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jaxen.ui.server import Handler, build_command, request_line
from jaxen.jsonl_protocol import validate_request


def test_build_command_uses_cli_jsonl_mode():
    command = build_command("/py", "/repo", "ask", "deepseek-v4-pro")
    assert command == ["/py", "-m", "jaxen", "--jsonl", "--cwd", "/repo", "--approval", "ask",
                       "--model", "deepseek-v4-pro"]
    with pytest.raises(ValueError):
        build_command("/py", "/repo", "yolo")


def test_request_line_is_a_valid_protocol_request():
    line = request_line("", "session-1", [{"inputId": "input_approval_x", "approved": True}])
    request = validate_request(json.loads(line))
    assert request["sessionId"] == "session-1" and request["responses"][0]["approved"] is True


@pytest.fixture
def server(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("", encoding="utf-8")
    (repo / ".jaxen").mkdir()
    (repo / ".jaxen" / "state.json").write_text("{}", encoding="utf-8")

    # 假 agent：忽略命令行参数，读一行请求，按 JSONL 协议吐两个事件和一个 turn
    fake = tmp_path / "fake_python"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'kind': 'event', 'type': 'operation.started', 'operationId': 'op_1', 'name': 'read_file', 'input': {'path': 'a.py'}}))\n"
        "print(json.dumps({'kind': 'event', 'type': 'message', 'role': 'assistant', 'content': 'echo: ' + request['text']}))\n"
        "print(json.dumps({'kind': 'turn', 'status': 'completed', 'sessionId': 's1', 'usage': {}}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.default_workspace = repo
    httpd.python = str(fake)
    httpd.running = {}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_diff_lists_changes_but_hides_agent_state_dir(server):
    data = json.load(urllib.request.urlopen(server + "/api/diff"))
    assert "+x = 2" in data["diff"]
    assert data["untracked"] == ["new.py"]  # .jaxen/ 是 agent 自己的目录，不算用户改动


def test_turn_streams_agent_events_line_by_line(server):
    body = json.dumps({"text": "hi", "approval": "auto"}).encode()
    request = urllib.request.Request(server + "/api/turn", data=body, headers={"Content-Type": "application/json"})
    rows = [json.loads(line) for line in urllib.request.urlopen(request).read().decode().splitlines() if line]
    assert rows[0]["kind"] == "ui" and rows[0]["turnId"]
    assert [row.get("type") or row["kind"] for row in rows[1:]] == ["operation.started", "message", "turn"]
    assert rows[2]["content"] == "echo: hi"


def test_turn_rejects_unknown_approval_mode(server):
    body = json.dumps({"text": "hi", "approval": "yolo"}).encode()
    request = urllib.request.Request(server + "/api/turn", data=body, headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 400


def test_serves_built_app_and_blocks_path_traversal(server, tmp_path, monkeypatch):
    import jaxen.ui.server as ui

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(ui, "DIST", dist)

    assert urllib.request.urlopen(server + "/").read() == b"<div id=root></div>"
    asset = urllib.request.urlopen(server + "/assets/app.js")
    assert asset.headers["Content-Type"] == "text/javascript"
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(server + "/assets/../../secret.txt")
    assert error.value.code == 404


def test_list_dirs_shows_subfolders_only(tmp_path):
    from jaxen.ui.server import list_dirs

    (tmp_path / "proj" / ".git").mkdir(parents=True)
    (tmp_path / "notes").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    listing = list_dirs(str(tmp_path))
    assert [d["name"] for d in listing["dirs"]] == ["notes", "proj"]  # 不含文件和隐藏目录
    assert next(d for d in listing["dirs"] if d["name"] == "proj")["isRepo"] is True
    assert listing["parent"] == str(tmp_path.resolve().parent)
    with pytest.raises(ValueError):
        list_dirs(str(tmp_path / "file.txt"))


def test_list_files_respects_gitignore_and_query(server):
    data = json.load(urllib.request.urlopen(server + "/api/files?q=PY"))
    assert data["files"] == ["a.py", "new.py"]  # .jaxen/ 不出现；查询不区分大小写


def test_upload_saves_into_jaxen_uploads_without_path_escape(server, tmp_path):
    import base64

    def upload(name, data=b"hello"):
        body = json.dumps({"name": name, "data": base64.b64encode(data).decode()}).encode()
        request = urllib.request.Request(server + "/api/upload", data=body, headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(request))["path"]

    assert upload("spec.md") == ".jaxen/uploads/spec.md"
    assert upload("spec.md") == ".jaxen/uploads/spec-1.md"  # 同名不覆盖
    assert upload("../../evil.sh") == ".jaxen/uploads/evil.sh"  # 只取文件名，不能写到工作区外
    assert (tmp_path / "repo" / ".jaxen" / "uploads" / "evil.sh").read_bytes() == b"hello"
    assert not (tmp_path / "evil.sh").exists()
