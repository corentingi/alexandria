"""Download a file from a URL to attach to a resource (design §8.5)."""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from alexandria.config import Settings
from alexandria.fetchers.base import download
from alexandria.security import sanitize_filename


@dataclass
class DownloadedFile:
    path: Path
    filename: str  # sanitized
    mime: str
    final_url: str


_DISPOSITION = re.compile(r"""filename\*?=(?:UTF-8'')?"?([^";]+)"?""", re.IGNORECASE)


async def download_attachment(url: str, staging: Path, settings: Settings) -> DownloadedFile:
    dest = staging / "download.tmp"
    max_bytes = settings.attachments.max_mb * 1_000_000
    result = await download(url, dest, settings, max_bytes)
    mime = result.content_type.split(";", 1)[0].strip().lower() or "application/octet-stream"
    name = _filename(result.headers.get("content-disposition"), result.final_url)
    if "." not in name:
        name += mimetypes.guess_extension(mime) or ""
    return DownloadedFile(dest, sanitize_filename(name), mime, result.final_url)


def _filename(disposition: str | None, url: str) -> str:
    if disposition:
        match = _DISPOSITION.search(disposition)
        if match:
            return unquote(match.group(1))
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download"
