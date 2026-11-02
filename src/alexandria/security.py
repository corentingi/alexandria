"""SSRF guard and path safety (design §12)."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

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
    parts = urlsplit(url)
    if parts.scheme not in set(schemes):
        raise BlockedURL(f"scheme {parts.scheme!r} is not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise BlockedURL("URL has no host")
    if host in {h.lower() for h in allow_private_hosts}:
        return
    port = parts.port or (443 if parts.scheme == "https" else 80)
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
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if is_forbidden_ip(ip):
            raise BlockedURL(f"host {host!r} resolves to a non-public address ({ip})")


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
