"""SSRF guard and path safety (design §12)."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

from httpcore import AsyncNetworkBackend, AsyncNetworkStream
from httpcore._backends.base import SOCKET_OPTION

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}


class BlockedURL(ValueError):
    """The URL points at a host the SSRF guard does not allow. Never retried."""


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


# Swapped in tests so the suite never touches DNS.
_resolver: Resolver = system_resolver


def set_resolver(resolver: Resolver) -> Resolver:
    global _resolver
    previous, _resolver = _resolver, resolver
    return previous


def is_forbidden_ip(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return is_forbidden_ip(ip.ipv4_mapped)
    return (
        ip in _METADATA
        or ip.is_loopback
        or ip.is_private  # RFC 1918, IPv6 ULA, and other non-global ranges
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT)
        or not ip.is_global
    )


async def check_url(
    url: str,
    allow_private_hosts: Iterable[str] = (),
    schemes: Iterable[str] = ("http", "https"),
) -> None:
    """Raise ``BlockedURL`` unless every address ``url``'s host resolves to is public."""
    await pinned_address(url, allow_private_hosts, schemes)


async def pinned_address(
    url: str,
    allow_private_hosts: Iterable[str] = (),
    schemes: Iterable[str] = ("http", "https"),
) -> str | None:
    """Check ``url`` like ``check_url`` and return the address to connect to.

    Connecting by name would resolve the host a second time, and a DNS server answering
    a public address to the check and ``127.0.0.1`` to the connect (DNS rebinding) would
    get through. Callers connect to this address instead. ``None``: an allow-listed host,
    connected to by name.
    """
    parts = urlsplit(url)
    if parts.scheme not in set(schemes):
        raise BlockedURL(f"scheme {parts.scheme!r} is not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise BlockedURL("URL has no host")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return await _host_address(host, port, allow_private_hosts)


async def _host_address(host: str, port: int, allow_private_hosts: Iterable[str]) -> str | None:
    host = host.lower().rstrip(".")
    if host in {h.lower() for h in allow_private_hosts}:
        return None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise BlockedURL(f"host {host!r} is not allowed")
        try:
            addresses = await _resolver(host, port)
        except OSError as exc:
            raise BlockedURL(f"cannot resolve {host!r}: {exc}") from exc
        if not addresses:
            raise BlockedURL(f"cannot resolve {host!r}")
    checked = []
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if is_forbidden_ip(ip):
            raise BlockedURL(f"host {host!r} resolves to a non-public address ({ip})")
        checked.append(str(ip))
    return checked[0]


class GuardedBackend(AsyncNetworkBackend):
    """httpcore network backend that resolves and checks every host itself, then connects
    to the checked address: a TLS connection still verifies the certificate against the
    host name, which httpcore passes separately. This is what makes the SSRF guard hold
    against DNS rebinding; the ``check_url`` calls before a request give clear errors."""

    def __init__(self, inner: AsyncNetworkBackend, allow_private_hosts: Iterable[str]) -> None:
        self.inner = inner
        self.allow = list(allow_private_hosts)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        address = await _host_address(host, port, self.allow) or host
        return await self.inner.connect_tcp(
            address, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )  # fmt: skip

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        raise BlockedURL("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name: str, default: str = "file") -> str:
    """Basename only, whitelisted characters, no leading dots."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _SAFE_CHARS.sub("_", base).strip("._") or default
    if len(base) > 120:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 120 - len(ext) - 1] + dot + ext) if dot and len(ext) <= 10 else base[:120]
    return base


def safe_join(root: Path, *parts: str) -> Path:
    """Join and verify the result stays inside ``root`` (after resolving symlinks)."""
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PermissionError(f"path escapes {root}")
    return candidate


def unique_name(directory: Path, name: str) -> str:
    """``name``, or ``stem-2.ext``, ``stem-3.ext``… if taken in ``directory``."""
    if not (directory / name).exists():
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while True:
        candidate = f"{stem}-{n}.{ext}" if dot else f"{stem}-{n}"
        if not (directory / candidate).exists():
            return candidate
        n += 1
