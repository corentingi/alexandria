"""An HTTP proxy on 127.0.0.1 that applies the SSRF guard to programs Alexandria runs.

yt-dlp opens its own connections, to dozens of hosts for one video (the page, APIs, CDNs,
stream fragments). Python code in this process goes through ``GuardedBackend``; a child
process does not. Given ``--proxy``, yt-dlp sends every request through this proxy, which
resolves and checks each host and connects to the address it checked, like
``GuardedBackend`` (ADR-0022). It also counts the bytes received, and stops a job that
downloads more than it should.

One proxy per job: ``async with GuardedProxy(...) as proxy: run(..., proxy.url)``. A
random password keeps other local processes from using it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import secrets
from collections.abc import Iterable
from types import TracebackType
from urllib.parse import urlsplit

from alexandria.security import BlockedURL, host_address

log = logging.getLogger(__name__)

_HEAD_LIMIT = 64 * 1024
_HOP_HEADERS = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}


class GuardedProxy:
    def __init__(
        self,
        allow_private_hosts: Iterable[str] = (),
        max_bytes: int | None = None,
        idle_timeout_s: float = 120,
        connect_timeout_s: float = 30,
    ) -> None:
        self.allow = list(allow_private_hosts)
        self.max_bytes = max_bytes
        self.idle_timeout_s = idle_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.received = 0  # bytes received from the internet, all connections
        self.over_limit = False
        self.blocked: list[str] = []  # why requests were refused by the SSRF guard
        self._password = secrets.token_urlsafe(18)
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        assert self._server is not None, "use the proxy inside 'async with'"
        port = self._server.sockets[0].getsockname()[1]
        return f"http://alexandria:{self._password}@127.0.0.1:{port}"

    async def __aenter__(self) -> GuardedProxy:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=_HEAD_LIMIT)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._server is not None
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._server.wait_closed()

    # -- one client connection --------------------------------------------------------

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError:
            pass
        except Exception:
            log.exception("proxy connection failed")
        finally:
            self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with asyncio.timeout(self.idle_timeout_s):
            head = await reader.readuntil(b"\r\n\r\n")
        request_line, *header_lines = head.decode("latin-1").split("\r\n")[:-2]
        try:
            method, target, version = request_line.split(" ")
        except ValueError:
            return await _reply(writer, 400, "Bad Request")
        headers = [line.split(":", 1) for line in header_lines if ":" in line]
        if not self._authorized(headers):
            return await _reply(writer, 407, "Proxy Authentication Required",
                                'Proxy-Authenticate: Basic realm="alexandria"\r\n')  # fmt: skip
        if self.over_limit:
            return await _reply(writer, 403, "Download limit reached")
        if method == "CONNECT":
            host, _, port_text = target.rpartition(":")
            host = host.strip("[]")
            if not host or not port_text.isdigit():
                return await _reply(writer, 400, "Bad Request")
            upstream = await self._open(writer, host, int(port_text))
            if upstream is None:
                return None
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            return await self._relay(reader, writer, *upstream)
        # A plain http:// request, in absolute form. Forwarded once: the connection is
        # closed after it, so a second request cannot reuse it for another host.
        parts = urlsplit(target)
        if parts.scheme != "http" or not parts.hostname:
            return await _reply(writer, 400, "Bad Request")
        upstream = await self._open(writer, parts.hostname, parts.port or 80)
        if upstream is None:
            return None
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        kept = "".join(
            f"{name}:{value}\r\n" for name, value in headers
            if name.strip().lower() not in _HOP_HEADERS
        )  # fmt: skip
        upstream[1].write(f"{method} {path} {version}\r\n{kept}Connection: close\r\n\r\n"
                          .encode("latin-1"))  # fmt: skip
        return await self._relay(reader, writer, *upstream)

    def _authorized(self, headers: list[list[str]]) -> bool:
        expected = base64.b64encode(f"alexandria:{self._password}".encode()).decode()
        for name, value in headers:
            if name.strip().lower() == "proxy-authorization":
                scheme, _, credentials = value.strip().partition(" ")
                if scheme.lower() == "basic" and secrets.compare_digest(
                    credentials.strip(), expected
                ):
                    return True
        return False

    async def _open(
        self, writer: asyncio.StreamWriter, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        try:
            address = await host_address(host, port, self.allow) or host
        except BlockedURL as exc:
            self.blocked.append(str(exc))
            await _reply(writer, 403, f"Blocked: {exc}")
            return None
        try:
            async with asyncio.timeout(self.connect_timeout_s):
                return await asyncio.open_connection(address, port)
        except (OSError, TimeoutError) as exc:
            await _reply(writer, 502, f"Cannot connect: {exc}")
            return None

    async def _relay(
        self,
        client_r: asyncio.StreamReader,
        client_w: asyncio.StreamWriter,
        upstream_r: asyncio.StreamReader,
        upstream_w: asyncio.StreamWriter,
    ) -> None:
        up = asyncio.create_task(self._pipe(client_r, upstream_w, count=False))
        down = asyncio.create_task(self._pipe(upstream_r, client_w, count=True))
        try:
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if down not in done:  # the client finished sending; the answer may still come
                await down
        finally:
            for task in (up, down):
                task.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
            upstream_w.close()

    async def _pipe(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, count: bool
    ) -> None:
        try:
            while True:
                async with asyncio.timeout(self.idle_timeout_s):
                    chunk = await reader.read(65536)
                if not chunk:
                    break
                if count:
                    self.received += len(chunk)
                    if self.max_bytes is not None and self.received > self.max_bytes:
                        self.over_limit = True
                        break
                writer.write(chunk)
                await writer.drain()
        except OSError, TimeoutError:
            pass
        finally:
            if not count and writer.can_write_eof():
                with contextlib.suppress(OSError):
                    writer.write_eof()


async def _reply(writer: asyncio.StreamWriter, status: int, reason: str, extra: str = "") -> None:
    body = reason.encode("utf-8", "replace")
    reason_line = reason.encode("ascii", "replace").decode().replace("\r", " ").replace("\n", " ")
    writer.write(
        f"HTTP/1.1 {status} {reason_line[:200]}\r\n{extra}Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("latin-1")
        + body
    )
    with contextlib.suppress(OSError):
        await writer.drain()
