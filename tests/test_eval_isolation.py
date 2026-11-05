"""SWE-bench 评测隔离：出口代理只放行模型 API。

问题：评测容器能联网，agent 从 GitHub、PyPI 下载了上游修复照抄（pypa/build-1027 的补丁连 git 文件哈希都和官方一致）。
改法：容器放进 --internal 网络，唯一出口是 egress_proxy.py，只接受白名单里的 CONNECT 目标。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "swebench"))
from egress_proxy import handle, parse_connect, parse_http_forward  # noqa: E402


@pytest.mark.parametrize("head, expected", [
    ("CONNECT api.deepseek.com:443 HTTP/1.1", ("api.deepseek.com", 443)),
    ("connect API.DeepSeek.com:443 HTTP/1.0", ("api.deepseek.com", 443)),
    ("GET http://pypi.org/simple/ HTTP/1.1", None),  # 不是 CONNECT；普通 HTTP 由 parse_http_forward 处理
    ("CONNECT pypi.org HTTP/1.1", None),
    ("CONNECT pypi.org:https HTTP/1.1", None),
    ("", None),
])
def test_parse_connect(head, expected):
    assert parse_connect(head) == expected


def _ask(request, allowed, upstream_port=None):
    async def scenario():
        upstream = None
        if upstream_port is not None:
            async def echo(reader, writer):
                writer.write(await reader.read(100))
                await writer.drain()
                writer.close()
            upstream = await asyncio.start_server(echo, "127.0.0.1", upstream_port)
        proxy = await asyncio.start_server(lambda r, w: handle(r, w, allowed), "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        status = await reader.readline()
        echoed = b""
        if b" 200 " in status:
            await reader.readline()  # 空行
            writer.write(b"ping")
            await writer.drain()
            echoed = await reader.read(100)
        writer.close()
        proxy.close()
        if upstream:
            upstream.close()
        return status, echoed

    return asyncio.run(scenario())


def test_proxy_denies_hosts_outside_allowlist():
    status, _ = _ask(b"CONNECT raw.githubusercontent.com:443 HTTP/1.1\r\n\r\n", {("api.deepseek.com", 443)})
    assert b"403" in status


def test_proxy_tunnels_allowlisted_host(unused_tcp_port):
    status, echoed = _ask(f"CONNECT 127.0.0.1:{unused_tcp_port} HTTP/1.1\r\n\r\n".encode(),
                          {("127.0.0.1", unused_tcp_port)}, upstream_port=unused_tcp_port)
    assert b"200" in status and echoed == b"ping"


@pytest.fixture
def unused_tcp_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_parse_http_forward():
    assert parse_http_forward("POST http://llm-gw.jd.local/anthropic/v1/messages HTTP/1.1") == (("llm-gw.jd.local", 80), "/anthropic/v1/messages")
    assert parse_http_forward("GET http://h:8080/a?b=1 HTTP/1.1") == (("h", 8080), "/a?b=1")
    assert parse_http_forward("CONNECT h:443 HTTP/1.1") is None
    assert parse_http_forward("GET /relative HTTP/1.1") is None


def test_proxy_forwards_plain_http_to_allowlisted_host(unused_tcp_port):
    """普通 HTTP（公司内网的 http 模型网关）：请求行改回路径形式、去掉 Proxy-* 头后转给上游。"""
    async def scenario():
        seen = {}

        async def upstream(reader, writer):
            seen["head"] = (await reader.readuntil(b"\r\n\r\n")).decode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
            writer.close()

        up = await asyncio.start_server(upstream, "127.0.0.1", unused_tcp_port)
        proxy = await asyncio.start_server(lambda r, w: handle(r, w, {("127.0.0.1", unused_tcp_port)}), "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
        writer.write(f"POST http://127.0.0.1:{unused_tcp_port}/v1/messages HTTP/1.1\r\nHost: x\r\n"
                     "Proxy-Connection: keep-alive\r\n\r\n".encode())
        await writer.drain()
        response = await reader.read(100)
        writer.close()
        proxy.close()
        up.close()
        return seen["head"], response

    head, response = asyncio.run(scenario())
    assert head.startswith("POST /v1/messages HTTP/1.1") and "Proxy-Connection" not in head
    assert response.endswith(b"ok")


def test_proxy_denies_plain_http_outside_allowlist():
    status, _ = _ask(b"GET http://pypi.org/simple/ HTTP/1.1\r\nHost: pypi.org\r\n\r\n", {("api.deepseek.com", 443)})
    assert b"403" in status


def test_shell_commands_inherit_proxy_but_not_secrets(tmp_path, monkeypatch):
    # shell 子进程要带上代理：评测里 agent 的 pip/curl 才会经过出口代理、被拒绝并计入 egress_denied
    # （之前不带代理，请求在内网直接 DNS 失败，计数一直是 0）；公司网络里日常用 pip 也靠它。密钥仍然不继承。
    from jaxen.agent import CodingAgent
    from jaxen.providers import FakeModelClient
    from jaxen.session_store import SessionStore
    from jaxen.workspace import WorkspaceContext

    monkeypatch.setenv("HTTPS_PROXY", "http://jaxen-eval-proxy:8888")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("JAXEN_DEEPSEEK_API_KEY", "sk-test-not-real")
    agent = CodingAgent(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path, repo_root_override=tmp_path),
        session_store=SessionStore(tmp_path / ".jaxen" / "sessions"),
    )
    env = agent.shell_env()
    assert env["HTTPS_PROXY"] == "http://jaxen-eval-proxy:8888" and env["NO_PROXY"] == "localhost"
    assert "JAXEN_DEEPSEEK_API_KEY" not in env
