from __future__ import annotations

import asyncio
import base64

from alexandria.proxy import GuardedProxy


async def _echo_server() -> tuple[asyncio.Server, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.read(65536)
        if head.startswith(b"GET "):
            body = head.split(b"\r\n", 1)[0]
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        else:
            writer.write(b"echo:" + head)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _auth(proxy: GuardedProxy) -> str:
    userinfo = proxy.url.split("//", 1)[1].rsplit("@", 1)[0]
    return "Proxy-Authorization: Basic " + base64.b64encode(userinfo.encode()).decode()


async def _ask(proxy: GuardedProxy, request: str, then: bytes = b"") -> bytes:
    port = int(proxy.url.rsplit(":", 1)[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request.encode() + then)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), 5)
    if then and data.startswith(b"HTTP/1.1 200 Connection"):
        data += await asyncio.wait_for(reader.read(65536), 5)
    writer.close()
    return data


class TestGuardedProxy:
    async def test_tunnels_to_an_allowed_host(self) -> None:
        server, port = await _echo_server()
        async with server, GuardedProxy(allow_private_hosts=["127.0.0.1"]) as proxy:
            data = await _ask(
                proxy, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n{_auth(proxy)}\r\n\r\n", b"ping"
            )
        assert data.startswith(b"HTTP/1.1 200") and data.endswith(b"echo:ping")
        assert proxy.received == len(b"echo:ping")

    async def test_refuses_private_addresses(self) -> None:
        async with GuardedProxy() as proxy:
            for target in ("127.0.0.1:443", "intranet.example:443", "[::1]:443"):
                data = await _ask(proxy, f"CONNECT {target} HTTP/1.1\r\n{_auth(proxy)}\r\n\r\n")
                assert data.startswith(b"HTTP/1.1 403"), data
            data = await _ask(proxy, f"GET http://127.0.0.1/ HTTP/1.1\r\n{_auth(proxy)}\r\n\r\n")
            assert data.startswith(b"HTTP/1.1 403")
        assert len(proxy.blocked) == 4 and "non-public" in proxy.blocked[1]

    async def test_needs_its_password(self) -> None:
        async with GuardedProxy(allow_private_hosts=["127.0.0.1"]) as proxy:
            data = await _ask(proxy, "CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
            assert data.startswith(b"HTTP/1.1 407")
            wrong = "Proxy-Authorization: Basic " + base64.b64encode(b"alexandria:x").decode()
            data = await _ask(proxy, f"CONNECT 127.0.0.1:1 HTTP/1.1\r\n{wrong}\r\n\r\n")
            assert data.startswith(b"HTTP/1.1 407")

    async def test_forwards_plain_http_in_origin_form(self) -> None:
        server, port = await _echo_server()
        async with server, GuardedProxy(allow_private_hosts=["127.0.0.1"]) as proxy:
            data = await _ask(
                proxy,
                f"GET http://127.0.0.1:{port}/a?b=1 HTTP/1.1\r\nHost: x\r\n{_auth(proxy)}\r\n\r\n",
            )
        assert data.startswith(b"HTTP/1.1 200") and data.endswith(b"GET /a?b=1 HTTP/1.1")

    async def test_stops_after_the_byte_limit(self) -> None:
        server, port = await _echo_server()
        async with server, GuardedProxy(["127.0.0.1"], max_bytes=4) as proxy:
            request = f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n{_auth(proxy)}\r\n\r\n"
            data = await _ask(proxy, request, b"ping")
            assert b"echo" not in data and proxy.over_limit
            assert (await _ask(proxy, request)).startswith(b"HTTP/1.1 403")
