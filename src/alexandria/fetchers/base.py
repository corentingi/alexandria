"""Fetcher contract and the guarded HTTP download shared by every fetcher."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx

from alexandria.config import Settings
from alexandria.models import Artifact, Resource
from alexandria.security import BlockedURL, check_url
from alexandria.walls import AFTER_TIP, site_warning


class FetchError(Exception):
    """A fetch failure. ``permanent`` errors are not retried."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class PermanentFetchError(FetchError):
    def __init__(self, message: str) -> None:
        super().__init__(message, permanent=True)


@dataclass
class SnapshotResult:
    """What a fetcher produced in its staging dir."""

    artifacts: list[Artifact]
    meta: dict[str, Any] = field(default_factory=dict)
    title: str | None = None
    # Role of the artifact to chunk for search ("content", "readme"), if any.
    text_role: str | None = None


class Fetcher(Protocol):
    async def fetch(self, resource: Resource, staging_dir: Path) -> SnapshotResult: ...


_PERMANENT_STATUS = {400, 401, 403, 404, 405, 410, 414, 451}
_SELECTED_HEADERS = (
    "content-type", "content-length", "last-modified", "etag", "server", "content-language",
    "content-disposition",
)  # fmt: skip


@dataclass
class Download:
    final_url: str
    status_code: int
    content_type: str
    size: int
    headers: dict[str, str]
    redirects: list[str]
    elapsed_s: float

    def fetch_json(self, requested_url: str) -> dict[str, Any]:
        return {
            "requested_url": requested_url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "size": self.size,
            "redirects": self.redirects,
            "headers": self.headers,
            "elapsed_s": round(self.elapsed_s, 3),
        }


def http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.fetch.timeout_s,
        follow_redirects=False,  # we follow by hand to apply the SSRF guard on every hop
        headers={"User-Agent": settings.fetch.user_agent},
    )


async def download(
    url: str,
    dest: Path,
    settings: Settings,
    max_bytes: int,
    client: httpx.AsyncClient | None = None,
) -> Download:
    """Stream ``url`` into ``dest``. Applies the SSRF guard on each redirect hop."""
    own_client = client is None
    client = client or http_client(settings)
    redirects: list[str] = []
    started = time.monotonic()
    current = url
    try:
        for _hop in range(settings.fetch.max_redirects + 1):
            try:
                await check_url(current, settings.security.allow_private_hosts)
            except BlockedURL as exc:
                raise PermanentFetchError(f"blocked by SSRF guard: {exc}") from exc
            try:
                async with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise PermanentFetchError("redirect without a Location header")
                        current = urljoin(current, location)
                        redirects.append(current)
                        continue
                    _raise_for_status(response)
                    size = 0
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise PermanentFetchError(f"response exceeds the {max_bytes} byte cap")
                    with dest.open("wb") as fh:
                        async for block in response.aiter_bytes(1 << 16):
                            size += len(block)
                            if size > max_bytes:
                                raise PermanentFetchError(
                                    f"response exceeds the {max_bytes} byte cap"
                                )
                            fh.write(block)
                    return Download(
                        final_url=str(response.url),
                        status_code=response.status_code,
                        content_type=response.headers.get("content-type", ""),
                        size=size,
                        headers={
                            k: response.headers[k]
                            for k in _SELECTED_HEADERS
                            if k in response.headers
                        },
                        redirects=redirects,
                        elapsed_s=time.monotonic() - started,
                    )
            except httpx.TimeoutException as exc:
                raise FetchError(f"timeout fetching {current}") from exc
            except httpx.TransportError as exc:
                raise FetchError(f"network error fetching {current}: {exc}") from exc
        raise PermanentFetchError(f"more than {settings.fetch.max_redirects} redirects")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        if own_client:
            await client.aclose()


def _raise_for_status(response: httpx.Response) -> None:
    code = response.status_code
    if code < 400:
        return
    message = f"HTTP {code} from {response.url}"
    if code in (401, 403, 429) and (warning := site_warning(str(response.url))):
        message = f"{message}: {warning.message.split('. ', 1)[0]}. {AFTER_TIP}"
    if code in _PERMANENT_STATUS:
        raise PermanentFetchError(message)
    raise FetchError(message)
