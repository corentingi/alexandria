"""Shapes returned by the REST API and MCP. One place, so both interfaces agree."""

from __future__ import annotations

import re
from typing import Any

from alexandria.models import Resource, iso
from alexandria.storage.index import HL_END, HL_START, Job, SearchHit


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` and empty containers; agents pay per token."""
    return {k: v for k, v in data.items() if v is not None and v != [] and v != {}}


_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)?")
_NOISE = re.compile(r"(^|\n)\s*#{1,6}\s+|[`*_]{1,3}(?=\S)|(?<=\S)[`*_]{1,3}")


def plain_snippet(snippet: str) -> str:
    """Readable snippet text: link labels without URLs, no heading or emphasis marks."""
    # The chunk's own heading is reported separately (heading_path): drop it here.
    text = (
        re.sub(r"^\s*#{1,6}[^\n]*\n", "", snippet) if snippet.lstrip().startswith("#") else snippet
    )
    text = _LINK.sub(r"\1", text)
    text = _NOISE.sub(lambda m: " " if m.group(1) is not None else "", text)
    return " ".join(text.split())


def highlight(snippet: str | None, start: str = "**", end: str = "**") -> str | None:
    if snippet is None:
        return None
    return plain_snippet(snippet).replace(HL_START, start).replace(HL_END, end)


def search_result(hit: SearchHit, row: dict[str, Any]) -> dict[str, Any]:
    snippet = highlight(hit.snippet)
    if snippet is None:
        snippet = (row.get("why") or "")[:200] or None
    return _compact(
        {
            "id": row["id"],
            "slug": row["slug"],
            "kind": row["kind"],
            "title": row["title"],
            "url": row.get("canonical_url"),
            "why": row.get("why"),
            "tags": row.get("tags") or [],
            "status": row["status"],
            "fetch_status": row["fetch_status"],
            "project": row.get("project"),
            "captured_at": row["captured_at"],
            "snippet": snippet,
            "chunk": _compact({"ord": hit.chunk_ord, "heading_path": hit.heading_path or None})
            if hit.chunk_ord is not None
            else None,
        }
    )


def resource_detail(
    resource: Resource, chunks: list[dict[str, Any]], full: bool = True
) -> dict[str, Any]:
    """Metadata, why, notes, chunk list and files. Never the content itself."""
    snapshots = [
        {
            "id": s.id,
            "artifacts": [
                _compact(
                    {
                        "role": a.role,
                        "path": f"snapshots/{s.id}/{a.path}",
                        "mime": a.mime,
                        "size": a.size,
                        "sha256": a.sha256 if full else None,
                        **(a.model_extra or {}),
                    }
                )
                for a in s.artifacts
            ],
        }
        for s in resource.snapshots
    ]
    if not full:  # agents only need the current snapshot
        snapshots = [s for s in snapshots if s["id"] == resource.current_snapshot]
    return _compact(
        {
            "id": resource.id,
            "slug": resource.slug,
            "kind": resource.kind.value,
            "title": resource.title,
            "source_url": resource.source_url,
            "canonical_url": resource.canonical_url,
            "why": resource.why,
            "notes": resource.notes if resource.kind.value != "note" else None,
            "tags": resource.tags,
            "scope": resource.scope.value,
            "project": resource.project,
            "status": resource.status.value,
            "fetch_status": resource.fetch_status.value,
            "fetch_error": resource.fetch_error,
            "captured_at": iso(resource.captured_at),
            "updated_at": iso(resource.updated_at),
            "created_by": resource.created_by,
            "meta": resource.meta,
            "current_snapshot": resource.current_snapshot,
            "snapshots": snapshots,
            "attachments": [
                _compact(
                    {
                        "path": f"attachments/{a.path}",
                        "mime": a.mime,
                        "size": a.size,
                        "sha256": a.sha256 if full else None,
                        "origin_url": a.origin_url,
                        "added_at": iso(a.added_at),
                        "added_by": a.added_by,
                    }
                )
                for a in resource.attachments
            ],
            "source_file": _compact(
                {
                    "path": f"source/{resource.source_file.path}",
                    "mime": resource.source_file.mime,
                    "size": resource.source_file.size,
                    "sha256": resource.source_file.sha256,
                }
            )
            if resource.source_file
            else None,
            "chunks": [
                _compact({"ord": c["ord"], "heading_path": c["heading_path"], "chars": c["chars"]})
                for c in chunks
            ],
            "generated": resource.generated,
        }
    )


def job_view(job: Job) -> dict[str, Any]:
    return _compact(
        {
            "id": job.id,
            "type": job.type,
            "resource_id": job.resource_id,
            "status": job.status,
            "attempts": job.attempts,
            "run_after": job.run_after if job.status == "queued" else None,
            "error": job.error,
            "payload": job.payload,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }
    )
