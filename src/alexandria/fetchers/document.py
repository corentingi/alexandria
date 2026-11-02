"""Document "fetcher": extracts text from the uploaded original in ``source/``.

Each run produces a snapshot with ``content.md`` (when text could be extracted) and
``extract.json``. The original is never modified, so a later, better extractor can be run
again with ``refetch``. A file that cannot be read still makes a valid snapshot: it stays
archived and downloadable, flagged ``meta.extraction: unsupported | failed``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from alexandria.config import Settings
from alexandria.extract import ExtractionError, detect_format, extract
from alexandria.fetchers.base import PermanentFetchError, SnapshotResult
from alexandria.models import Artifact, Resource
from alexandria.storage.files import FileStore, describe_file

POOR_EXTRACTION_CHARS = 200


class DocumentFetcher:
    def __init__(self, settings: Settings) -> None:
        self.files = FileStore(settings.data_dir)

    async def fetch(self, resource: Resource, staging_dir: Path) -> SnapshotResult:
        source = resource.source_file
        if source is None:
            raise PermanentFetchError("document has no source file")
        path = self.files.resource_dir(resource.id) / "source" / source.path
        if not path.is_file():
            raise PermanentFetchError(f"source file {source.path} is missing")
        return await extract_to(path, staging_dir, source.path, source.mime)


async def extract_to(
    path: Path,
    staging_dir: Path,
    filename: str | None,
    mime: str | None,
    url: str | None = None,
) -> SnapshotResult:
    """Extract ``path`` into ``staging_dir``: content.md + extract.json."""
    fmt = detect_format(path, filename, mime)
    meta: dict[str, Any] = {"format": fmt}
    report: dict[str, Any] = {"source": filename, "format": fmt, "mime": mime}
    title: str | None = None
    artifacts: list[Artifact] = []
    text_role: str | None = None
    if fmt is None:
        meta["extraction"] = "unsupported"
    else:
        try:
            result = await asyncio.to_thread(extract, path, fmt, url)
        except ExtractionError as exc:
            meta["extraction"] = "failed"
            meta["extraction_error"] = str(exc)
            report["error"] = str(exc)
        else:
            title = result.title
            meta.update(result.meta)
            report.update(result.meta)
            report["chars"] = len(result.markdown)
            if result.markdown:
                content = staging_dir / "content.md"
                content.write_text(result.markdown + "\n", encoding="utf-8")
                artifacts.append(describe_file(content, "content"))
                text_role = "content"
            # Short text is only suspicious where a layout format may hide it (a scanned
            # PDF without a text layer); a short Markdown or text file is just short.
            suspicious = fmt in ("pdf", "epub") and len(result.markdown) < POOR_EXTRACTION_CHARS
            meta["extraction"] = "poor" if suspicious else "ok"
    report["extraction"] = meta["extraction"]
    extract_json = staging_dir / "extract.json"
    extract_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    artifacts.append(describe_file(extract_json, "extract"))
    return SnapshotResult(artifacts=artifacts, meta=meta, title=title, text_role=text_role)
