"""Article fetcher (design §8.2): raw body, extracted Markdown, fetch metadata."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Any

import trafilatura
from trafilatura.metadata import extract_metadata

from alexandria.config import Settings
from alexandria.fetchers.base import SnapshotResult, download
from alexandria.fetchers.document import extract_to
from alexandria.models import Resource
from alexandria.storage.files import describe_file

POOR_EXTRACTION_CHARS = 500
_HTML_TYPES = ("text/html", "application/xhtml+xml")


class ArticleFetcher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def fetch(self, resource: Resource, staging_dir: Path) -> SnapshotResult:
        url = resource.source_url or resource.canonical_url
        assert url, "an article always has a URL"
        body_path = staging_dir / "body.tmp"
        result = await download(url, body_path, self.settings, self.settings.fetch.max_bytes)

        content_type = result.content_type.split(";", 1)[0].strip().lower()
        raw = body_path.read_bytes()
        is_html = content_type in _HTML_TYPES or (not content_type and _sniff_html(raw))
        meta: dict[str, Any] = {"content_type": content_type or None}
        title: str | None = None
        text_role: str | None = None
        artifacts = []

        if is_html:
            page = staging_dir / "page.html"
            body_path.rename(page)
            artifacts.append(describe_file(page, "page"))
            markdown, extracted = await asyncio.to_thread(_extract, raw, result.final_url)
            if markdown:
                content = staging_dir / "content.md"
                content.write_text(markdown + "\n", encoding="utf-8")
                artifacts.append(describe_file(content, "content"))
                text_role = "content"
            meta.update({k: v for k, v in extracted.items() if k != "title" and v})
            meta["extraction"] = (
                "ok" if markdown and len(markdown) >= POOR_EXTRACTION_CHARS else "poor"
            )
            title = extracted.get("title")
        else:
            ext = mimetypes.guess_extension(content_type) if content_type else None
            original = staging_dir / f"original{ext or _ext_from_url(result.final_url)}"
            body_path.rename(original)
            artifacts.append(describe_file(original, "original"))
            # PDF, DOCX, EPUB, plain text…: extract text when we can (the original stays).
            doc = await extract_to(
                original, staging_dir, result.final_url.rsplit("/", 1)[-1], content_type,
                result.final_url,
            )  # fmt: skip
            artifacts.extend(doc.artifacts)
            meta.update(doc.meta)
            title, text_role = doc.title, doc.text_role

        fetch_json = staging_dir / "fetch.json"
        fetch_json.write_text(json.dumps(result.fetch_json(url), indent=2), encoding="utf-8")
        artifacts.append(describe_file(fetch_json, "fetch"))
        if result.final_url != url:
            meta["final_url"] = result.final_url
        return SnapshotResult(artifacts=artifacts, meta=meta, title=title, text_role=text_role)


def _extract(raw: bytes, url: str) -> tuple[str | None, dict[str, Any]]:
    markdown = trafilatura.extract(
        raw,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_comments=False,
        include_formatting=True,
    )
    info: dict[str, Any] = {}
    tree = trafilatura.load_html(raw)
    if tree is not None:
        doc = extract_metadata(tree, default_url=url)
        if doc is not None:
            info = {
                "title": doc.title,
                "author": doc.author,
                "published_at": doc.date,
                "sitename": doc.sitename,
                "description": doc.description,
            }
        lang = tree.get("lang") or tree.get("xml:lang")
        if lang:
            info["language"] = lang.split("-")[0].lower()
    return (markdown.strip() if markdown else None), info


def _sniff_html(raw: bytes) -> bool:
    head = raw[:1024].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html"))


def _ext_from_url(url: str) -> str:
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()
        if ext.isascii() and len(ext) <= 6 and ext[1:].isalnum():
            return ext
    return ".bin"
