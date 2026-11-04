"""Document "fetcher": extracts text from the uploaded original in ``source/``.

Each run produces a snapshot with ``content.md`` (when text could be extracted) and
``extract.json``. The original is never modified, so a later, better extractor can be run
again with ``refetch``. A file that cannot be read still makes a valid snapshot: it stays
archived and downloadable, flagged ``meta.extraction: unsupported | failed``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alexandria.config import ExtractSettings, Settings
from alexandria.extract import Extraction, ExtractionError, detect_format
from alexandria.fetchers.base import PermanentFetchError, SnapshotResult
from alexandria.isolate import run_isolated
from alexandria.models import Artifact, Resource
from alexandria.storage.files import FileStore, describe_file

POOR_EXTRACTION_CHARS = 200


class DocumentFetcher:
    def __init__(self, settings: Settings) -> None:
        self.files = FileStore(settings.data_dir)
        self.limits = settings.extract

    async def fetch(self, resource: Resource, staging_dir: Path) -> SnapshotResult:
        source = resource.source_file
        if source is None:
            raise PermanentFetchError("document has no source file")
        path = self.files.resource_dir(resource.id) / "source" / source.path
        if not path.is_file():
            raise PermanentFetchError(f"source file {source.path} is missing")
        return await extract_to(path, staging_dir, source.path, source.mime, limits=self.limits)


def charset_of(content_type: str | None) -> str | None:
    """The ``charset`` parameter of a Content-Type, if any."""
    for param in (content_type or "").split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"')
    return None


async def extract_to(
    path: Path,
    staging_dir: Path,
    filename: str | None,
    mime: str | None,
    url: str | None = None,
    *,
    limits: ExtractSettings,
) -> SnapshotResult:
    """Extract ``path`` into ``staging_dir``: content.md + extract.json. A charset in
    ``mime`` (``text/plain; charset=shift_jis``) is used to decode text formats."""
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
            data = await run_isolated("extract", [str(path), fmt, url, charset_of(mime)], limits)
            result = Extraction(**data)
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
