"""评测容器的出口代理：只放行模型 API，其他一律拒绝并记日志。

agent 容器在 `--internal` 的 Docker 网络里，没有通往外网的路由；本代理同时接入内网和默认网络，
是它唯一的出口。支持 HTTPS 的 CONNECT 隧道和普通 HTTP 转发（如公司内网的 http 模型网关），
目标必须在白名单里。这样 agent 查不到上游修复（GitHub、PyPI 上的新版本），但仍能调用模型。

    python egress_proxy.py --port 8888 --allow api.deepseek.com:443 --allow llm-gw.jd.local:80
"""

import argparse
import asyncio
import time
from urllib.parse import urlsplit


def parse_connect(head):
    """'CONNECT host:443 HTTP/1.1' -> ('host', 443)；不是 CONNECT 或格式不对返回 None。"""
    parts = head.split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT" or ":" not in parts[1]:
        return None
    host, _, port = parts[1].rpartition(":")
    return (host.lower(), int(port)) if port.isdigit() else None


def parse_http_forward(head):
    """普通 HTTP 代理请求 'POST http://host[:port]/path HTTP/1.1' -> (('host', port), '/path')；否则 None。"""
    parts = head.split(" ")
    if len(parts) < 3 or parts[0].upper() == "CONNECT" or not parts[1].lower().startswith("http://"):
        return None
    url = urlsplit(parts[1])
    if not url.hostname:
        return None
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    return (url.hostname.lower(), url.port or 80), path


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def handle(reader, writer, allowed):
    try:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return
    first_line = head.splitlines()[0] if head else ""
    forward = parse_http_forward(first_line)
    target = forward[0] if forward else parse_connect(first_line)
    if target not in allowed:
        print(time.strftime("%H:%M:%S"), "DENY", head.splitlines()[0][:200] if head else "", flush=True)
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(*target)
    except OSError:
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        writer.close()
        return
    if forward:
        # 普通 HTTP：把绝对地址改回路径形式，去掉只给代理看的头，原样转给上游
        method, _, version = first_line.split(" ", 2)
        headers = [line for line in head.split("\r\n")[1:] if line and not line.lower().startswith("proxy-")]
        upstream_writer.write(("\r\n".join([f"{method} {forward[1]} {version}", *headers]) + "\r\n\r\n").encode("latin-1"))
    else:
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


async def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--allow", action="append", required=True, help="host:port, repeatable")
    args = parser.parse_args()
    allowed = {parse_connect(f"CONNECT {item}") for item in args.allow}
    server = await asyncio.start_server(lambda r, w: handle(r, w, allowed), "0.0.0.0", args.port)
    print("egress proxy allowing", sorted(allowed), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
