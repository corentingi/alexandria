"""The library: every operation the interfaces expose, in one place.

Writes always go to ``resource.md`` first (the source of truth), then to the index. A
per-resource asyncio lock serializes edits to one resource (API PATCH vs. the worker).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from alexandria.chunking import Chunk, chunk_markdown
from alexandria.config import Settings
from alexandria.extract import detect_format
from alexandria.fetchers.article import ArticleFetcher
from alexandria.fetchers.attachment import download_attachment
from alexandria.fetchers.base import Fetcher, FetchError, PermanentFetchError, SnapshotResult
from alexandria.fetchers.document import DocumentFetcher, extract_to
from alexandria.fetchers.git_repo import GitRepoFetcher
from alexandria.ids import is_id, new_id, unique_slug
from alexandria.models import (
    Attachment,
    FetchStatus,
    Kind,
    Resource,
    Scope,
    Snapshot,
    Status,
    TitleSource,
    check_meta,
    iso,
    normalize_tags,
    utcnow,
)
from alexandria.security import BlockedURL, check_url, safe_join, sanitize_filename, unique_name
from alexandria.storage.files import (
    RESOURCE_FILE,
    SNAPSHOT_FORMAT,
    FileStore,
    InvalidResourceFile,
    guess_mime,
    parse_resource,
    sha256_file,
)
from alexandria.storage.index import Index, Job, SearchFilters, SearchHit
from alexandria.transfer import (
    EXPORT_ID_RE,
    ArchiveError,
    ExportInfo,
    ExportWriter,
    LibraryImportReport,
    unpack,
)
from alexandria.urls import Detected, InvalidURL, detect

log = logging.getLogger(__name__)

# Meta keys that describe one snapshot; cleared before a new snapshot's meta is merged.
_SNAPSHOT_META_KEYS = {
    "extraction", "archive_skipped", "possible_duplicate_of", "final_url", "lfs",
    "content_type",
}  # fmt: skip

MAX_CONTENT_CHARS = 50_000
# Extracted text of attachments: attachments/.text/<name>.md (a dot name never collides
# with a sanitized attachment name).
ATTACHMENT_TEXT_DIR = ".text"


class NotFound(LookupError):
    pass


class InvalidInput(ValueError):
    pass


@dataclass
class IngestResult:
    resource: Resource
    duplicate: bool
    job_id: str | None = None
    # For a duplicate: what the request added to the existing resource ("why", "tags").
    added: list[str] = field(default_factory=list)


@dataclass
class Update:
    """Fields a human or an agent may change. ``None`` means "leave unchanged"."""

    why: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    status: Status | None = None
    title: str | None = None
    project: str | None = None
    scope: Scope | None = None


@dataclass
class ImportReport:
    created: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


MAX_IMPORT = 5000


@dataclass
class ReindexReport:
    indexed: int = 0
    errors: list[str] = field(default_factory=list)


JobHandler = Callable[[Job], Awaitable[None]]


class Library:
    def __init__(
        self,
        settings: Settings,
        fetchers: dict[Kind, Fetcher] | None = None,
    ) -> None:
        self.settings = settings
        self.files = FileStore(settings.data_dir)
        self.files.ensure_layout()
        self.index = Index(settings.db_path)
        # Given fetchers override the defaults (tests swap in local-git ones).
        defaults: dict[Kind, Fetcher] = {
            Kind.ARTICLE: ArticleFetcher(settings),
            Kind.GIT_REPO: GitRepoFetcher(settings),
            Kind.DOCUMENT: DocumentFetcher(settings),
        }
        self.fetchers = defaults | (fetchers or {})
        self._locks: dict[str, asyncio.Lock] = {}
        self._ingest_lock = asyncio.Lock()
        self.max_attempts = settings.worker.max_attempts

    def close(self) -> None:
        self.index.close()

    def lock(self, resource_id: str) -> asyncio.Lock:
        return self._locks.setdefault(resource_id, asyncio.Lock())

    # ---------------------------------------------------------------- reads

    def resolve(self, ref: str) -> str:
        resource_id = self.index.resolve(ref.strip())
        if resource_id is None:
            raise NotFound(f"no resource {ref!r}")
        return resource_id

    def get(self, ref: str) -> Resource:
        resource_id = self.resolve(ref)
        try:
            return self.files.read(resource_id)
        except FileNotFoundError as exc:
            raise NotFound(f"resource {ref!r} is indexed but its folder is missing") from exc

    def chunks(self, resource_id: str) -> list[dict[str, Any]]:
        return self.index.chunks(resource_id)

    def search(
        self,
        query: str = "",
        filters: SearchFilters | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[tuple[SearchHit, dict[str, Any]]]:
        filters = filters or SearchFilters()
        if filters.tags:
            filters.tags = normalize_tags(filters.tags)
        hits = self.index.search(query, filters, limit=max(1, min(limit, 200)), offset=offset)
        rows = self.index.rows([h.id for h in hits])
        return [(h, rows[h.id]) for h in hits if h.id in rows]

    def tags(self) -> list[tuple[str, int]]:
        return self.index.list_tags()

    def text(self, resource: Resource) -> tuple[str, str | None]:
        """Full text of the current content and the role it came from.

        When the fetched page has no usable text (blocked, failed, or poor extraction) and
        an attachment has extracted text, that text stands in, with role ``attachment``.
        """
        text, role = self._main_text(resource)
        if not text.strip() or _weak(resource):
            found = self.attachment_text(resource)
            if found is not None:
                return found[0], "attachment"
        return text, role

    def _main_text(self, resource: Resource) -> tuple[str, str | None]:
        if resource.kind == Kind.NOTE:
            return resource.notes, "note"
        for role in ("content", "readme"):
            text = self.files.read_artifact_text(resource, role)
            if text is not None:
                return text, role
        return "", None

    def attachment_text(self, resource: Resource) -> tuple[str, Attachment] | None:
        """The first attachment with extracted text, and that text."""
        for attachment in resource.attachments:
            text = self._read_attachment_text(resource.id, attachment)
            if text and text.strip():
                return text, attachment
        return None

    def _read_attachment_text(self, resource_id: str, attachment: Attachment) -> str | None:
        if not attachment.text_path:
            return None
        try:
            path = safe_join(self.files.attachments_dir(resource_id), attachment.text_path)
            return path.read_text(encoding="utf-8")
        except OSError, PermissionError:
            return None

    def content(
        self,
        ref: str,
        chunk: int | None = None,
        offset: int = 0,
        max_chars: int = 8000,
    ) -> dict[str, Any]:
        resource = self.get(ref)
        max_chars = max(100, min(max_chars, MAX_CONTENT_CHARS))
        if chunk is not None:
            chunks = self.index.chunks(resource.id)
            match = next((c for c in chunks if c["ord"] == chunk), None)
            if match is None:
                raise NotFound(f"resource {ref!r} has no chunk {chunk} (it has {len(chunks)})")
            text = match["text"]
            piece = text[offset : offset + max_chars]
            end = offset + len(piece)
            return {
                "chunk": chunk,
                "heading_path": match["heading_path"],
                "offset": offset,
                "total_chars": len(text),
                "next_offset": end if end < len(text) else None,
                "next_chunk": chunk + 1 if chunk + 1 < len(chunks) else None,
                "text": piece,
                "source_role": match["source_role"],
            }
        text, role = self.text(resource)
        offset = max(0, offset)
        piece = text[offset : offset + max_chars]
        end = offset + len(piece)
        return {
            "offset": offset,
            "total_chars": len(text),
            "next_offset": end if end < len(text) else None,
            "text": piece,
            "source_role": role,
        }

    # ---------------------------------------------------------------- writes

    async def ingest(
        self,
        url: str,
        why: str | None = None,
        tags: list[str] | None = None,
        scope: Scope | None = None,
        project: str | None = None,
        kind: Kind | None = None,
        title: str | None = None,
        created_by: str | None = None,
    ) -> IngestResult:
        try:
            detected = detect(url, kind)
        except InvalidURL as exc:
            raise InvalidInput(str(exc)) from exc
        await self._guard(detected.canonical_url)
        async with self._ingest_lock:
            existing = self.index.find_by_canonical_url(detected.canonical_url)
            if existing is None:
                return self._create_from_url(detected, url, why, tags, scope, project, title,
                                             created_by)  # fmt: skip
        return await self._add_to_existing(existing, why, tags)

    async def _add_to_existing(
        self, resource_id: str, why: str | None, tags: list[str] | None
    ) -> IngestResult:
        """Saving something already saved: the reason and tags typed are not lost. A missing
        ``why`` is filled in (an existing one is never replaced) and new tags are added."""
        resource = self.files.read(resource_id)
        changes, added = Update(), []
        if why and why.strip() and not resource.why:
            changes.why, added = why, ["why"]
        new_tags = [t for t in normalize_tags(tags or []) if t not in resource.tags]
        if new_tags:
            changes.tags, added = [*resource.tags, *new_tags], [*added, "tags"]
        if added:
            resource = await self.update(resource_id, changes)
        return IngestResult(resource, duplicate=True, added=added)

    def _create_from_url(
        self,
        detected: Detected,
        url: str,
        why: str | None,
        tags: list[str] | None,
        scope: Scope | None,
        project: str | None,
        title: str | None,
        created_by: str | None,
    ) -> IngestResult:
        """Caller holds ``_ingest_lock``. Synchronous: no other task runs in between."""
        now = utcnow()
        resource_id = new_id()
        if detected.forge == "github":
            base_title = f"{detected.owner}/{detected.repo}"
            slug_base = f"{detected.owner}-{detected.repo}"
        else:
            base_title = (title or "").strip() or _title_from_url(detected.canonical_url)
            slug_base = base_title
        scope, project = _scope(scope, project)
        resource = Resource(
            id=resource_id,
            slug=unique_slug(slug_base, self.index.slug_taken),
            kind=detected.kind,
            title=base_title,
            title_source=TitleSource.URL,
            source_url=url.strip(),
            canonical_url=detected.canonical_url,
            why=why,
            tags=tags or [],
            scope=scope,
            project=project,
            fetch_status=FetchStatus.PENDING,
            captured_at=now,
            updated_at=now,
            created_by=created_by,
            meta={"forge": detected.forge} if detected.forge else {},
        )
        resource.status = Status.KEPT if resource.why else Status.INBOX
        self.files.write(resource)
        self.index.upsert(resource, [])
        job_id = self._enqueue("fetch", resource.id)
        return IngestResult(resource, duplicate=False, job_id=job_id)

    async def ingest_file(
        self,
        filename: str,
        chunks: AsyncIterator[bytes],
        mime: str | None = None,
        why: str | None = None,
        tags: list[str] | None = None,
        scope: Scope | None = None,
        project: str | None = None,
        created_by: str | None = None,
    ) -> IngestResult:
        """Save an uploaded document. Text is extracted in the background (ADR-0012)."""
        max_bytes = self.settings.attachments.max_mb * 1_000_000
        staging = self.files.new_staging_dir(f"upload-{new_id()}")
        try:
            tmp = staging / "upload.tmp"
            size = 0
            with tmp.open("wb") as fh:
                async for block in chunks:
                    size += len(block)
                    if size > max_bytes:
                        raise InvalidInput(f"file exceeds {self.settings.attachments.max_mb} MB")
                    fh.write(block)
            if size == 0:
                raise InvalidInput("the file is empty")
            sha = await asyncio.to_thread(sha256_file, tmp)  # up to attachments.max_mb
            canonical = f"urn:sha256:{sha}"
            scope, project = _scope(scope, project)
            async with self._ingest_lock:
                existing = self.index.find_by_canonical_url(canonical)
                if existing is not None:
                    return await self._add_to_existing(existing, why, tags)
                now = utcnow()
                name = sanitize_filename(filename, default="document")
                title = _title_from_filename(filename)
                resource = Resource(
                    id=new_id(),
                    slug=unique_slug(title, self.index.slug_taken),
                    kind=Kind.DOCUMENT,
                    title=title,
                    title_source=TitleSource.URL,
                    canonical_url=canonical,
                    why=why,
                    tags=tags or [],
                    scope=scope,
                    project=project,
                    fetch_status=FetchStatus.PENDING,
                    captured_at=now,
                    updated_at=now,
                    created_by=created_by,
                    source_file=Attachment(
                        path=name,
                        mime=(mime if mime and mime != "application/octet-stream" else None)
                        or guess_mime(name),
                        size=size,
                        sha256=sha,
                        added_at=now,
                        added_by=created_by,
                    ),
                )
                resource.status = Status.KEPT if resource.why else Status.INBOX
                shutil.move(tmp, safe_join(self.files.source_dir(resource.id), name))
                self.files.write(resource)
                self.index.upsert(resource, [])
                job_id = self._enqueue("fetch", resource.id)
            return IngestResult(resource, duplicate=False, job_id=job_id)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def import_urls(
        self,
        text: str,
        why: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        created_by: str | None = None,
    ) -> ImportReport:
        """Ingest every link in a plain list or a browser bookmarks export, now (the CLI).
        The UI and the API queue the same work as a job: ``start_url_import``."""
        report = ImportReport()
        entries = _capped_entries(text, report)
        for entry in entries:
            await self._import_entry(entry, why, tags, project, created_by, report)
        return report

    def start_url_import(
        self,
        text: str,
        why: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
        created_by: str | None = None,
        name: str | None = None,
    ) -> tuple[str, int]:
        """Queue the import of every link in ``text``. Returns (job id, number of links).

        Thousands of links take minutes (a DNS check and a write each): too long for one
        HTTP request behind a proxy. The job records its position, so a restart resumes.
        """
        report = ImportReport()
        entries = _capped_entries(text, report)
        if not entries:
            raise InvalidInput("no links found")
        _scope(None, project)  # reject a bad project name now, not in the job
        payload = {
            "name": name or "pasted links", "entries": [list(e) for e in entries],
            "why": why, "tags": tags or [], "project": project, "created_by": created_by,
            "position": 0, "report": asdict(report),
        }  # fmt: skip
        return self._enqueue("import_urls", None, payload), len(entries)

    async def _job_import_urls(self, job: Job) -> None:
        payload = dict(job.payload)
        entries = payload["entries"]
        report = ImportReport(**payload["report"])
        for position in range(int(payload["position"]), len(entries)):
            url, tags, title = entries[position]
            await self._import_entry(
                (url, tags, title), payload["why"], payload["tags"], payload["project"],
                payload["created_by"], report,
            )  # fmt: skip
            if position % 50 == 49:
                payload |= {"position": position + 1, "report": asdict(report)}
                self.index.set_job_payload(job.id, payload)
        payload.pop("entries")
        payload |= {
            "links": len(entries), "position": len(entries),
            "report": {"created": len(report.created), "duplicates": len(report.duplicates),
                       "errors": report.errors[:50], "error_count": len(report.errors)},
        }  # fmt: skip
        self.index.set_job_payload(job.id, payload)

    async def _import_entry(
        self,
        entry: tuple[str, list[str], str | None],
        why: str | None,
        tags: list[str] | None,
        project: str | None,
        created_by: str | None,
        report: ImportReport,
    ) -> None:
        url, extra_tags, title = entry
        try:
            result = await self.ingest(
                url, why=why, tags=[*(tags or []), *extra_tags], project=project, title=title,
                created_by=created_by,
            )  # fmt: skip
        except InvalidInput as exc:
            report.errors.append(f"{url}: {exc}")
            return
        except Exception as exc:  # one bad link must not stop the thousands after it
            log.exception("import of %s failed", url)
            report.errors.append(f"{url}: {type(exc).__name__}: {exc}")
            return
        target = report.duplicates if result.duplicate else report.created
        target.append(result.resource.id)

    async def create_note(
        self,
        title: str,
        body: str,
        why: str | None = None,
        tags: list[str] | None = None,
        scope: Scope | None = None,
        project: str | None = None,
        created_by: str | None = None,
    ) -> Resource:
        title = title.strip()
        if not title:
            raise InvalidInput("a note needs a title")
        now = utcnow()
        scope, project = _scope(scope, project)
        async with self._ingest_lock:
            resource = Resource(
                id=new_id(),
                slug=unique_slug(title, self.index.slug_taken),
                kind=Kind.NOTE,
                title=title,
                title_source=TitleSource.HUMAN,
                why=why,
                tags=tags or [],
                scope=scope,
                project=project,
                fetch_status=FetchStatus.READY,
                captured_at=now,
                updated_at=now,
                created_by=created_by,
                notes=body,
            )
            resource.status = Status.KEPT if resource.why else Status.INBOX
            self.files.write(resource)
            chunks, role = self._chunks_for(resource)
            self.index.upsert(resource, chunks, role or "note")
        return resource

    async def update(self, ref: str, changes: Update) -> Resource:
        resource_id = self.resolve(ref)
        async with self.lock(resource_id):
            resource = self.files.read(resource_id)
            if changes.title is not None:
                if not changes.title.strip():
                    raise InvalidInput("title cannot be empty")
                resource.title = changes.title.strip()
                resource.title_source = TitleSource.HUMAN
            if changes.why is not None:
                had_why = bool(resource.why)
                resource.why = changes.why
                if resource.why and resource.status == Status.INBOX:
                    resource.status = Status.KEPT
                elif not resource.why and had_why and resource.status == Status.KEPT:
                    resource.status = Status.INBOX
            if changes.notes is not None:
                resource.notes = changes.notes
            if changes.tags is not None:
                resource.tags = changes.tags
            if changes.project is not None or changes.scope is not None:
                project = resource.project if changes.project is None else changes.project
                scope = changes.scope or (Scope.PROJECT if changes.project else resource.scope)
                if changes.project == "":
                    project, scope = None, changes.scope or Scope.GLOBAL
                resource.scope, resource.project = _scope(scope, project)
            if changes.status is not None:
                if changes.status == Status.KEPT and not resource.why:
                    raise InvalidInput("a resource needs a 'why' before it can be kept")
                resource.status = changes.status
            resource.updated_at = utcnow()
            self.files.write(resource)
            if resource.kind == Kind.NOTE and changes.notes is not None:
                chunks, role = self._chunks_for(resource)
                self.index.upsert(resource, chunks, role or "note")
            else:
                self.index.upsert(resource, None)
        return resource

    async def delete(self, ref: str) -> str:
        resource_id = self.resolve(ref)
        async with self.lock(resource_id):
            detached = self.files.detach(resource_id)
            self.index.delete(resource_id)
        self._locks.pop(resource_id, None)
        if detached is not None:  # gigabytes of snapshots: not on the event loop
            await asyncio.to_thread(shutil.rmtree, detached, True)
        return resource_id

    async def refetch(self, ref: str) -> str:
        resource = self.get(ref)
        if resource.kind == Kind.NOTE:
            raise InvalidInput("a note has no source to re-fetch")
        if self.index.pending_jobs(resource.id, "fetch"):
            raise InvalidInput("a fetch is already queued or running for this resource")
        async with self.lock(resource.id):
            resource = self.files.read(resource.id)
            resource.fetch_status = FetchStatus.PENDING
            resource.fetch_error = None
            self.files.write(resource)
            self.index.upsert(resource, None)
        return self._enqueue("fetch", resource.id)

    async def attach_upload(
        self,
        ref: str,
        filename: str,
        chunks: AsyncIterator[bytes],
        mime: str | None = None,
        added_by: str | None = None,
    ) -> Attachment:
        resource_id = self.resolve(ref)
        max_bytes = self.settings.attachments.max_mb * 1_000_000
        staging = self.files.new_staging_dir(f"upload-{new_id()}")
        tmp = staging / "upload.tmp"
        try:
            size = 0
            with tmp.open("wb") as fh:
                async for block in chunks:
                    size += len(block)
                    if size > max_bytes:
                        raise InvalidInput(
                            f"attachment exceeds {self.settings.attachments.max_mb} MB"
                        )
                    fh.write(block)
            return await self._store_attachment(resource_id, tmp, filename, mime, None, added_by)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def delete_snapshot(self, ref: str, snapshot_id: str) -> Resource:
        """Remove one snapshot and its files. The last remaining snapshot cannot go.

        Deleting the current snapshot makes the newest remaining one current. Like every
        write, ``resource.md`` changes first: a leftover folder is harmless, a snapshot
        listed but missing is not.
        """
        resource_id = self.resolve(ref)
        async with self.lock(resource_id):
            resource = self.files.read(resource_id)
            snapshot = resource.snapshot(snapshot_id) if snapshot_id else None
            if snapshot is None:
                raise NotFound(f"no snapshot {snapshot_id!r} on {ref}")
            try:
                folder = self.files.snapshot_dir(resource_id, snapshot.id)
            except (ValueError, PermissionError) as exc:
                raise InvalidInput(str(exc)) from exc
            if len(resource.snapshots) == 1:
                raise InvalidInput(
                    "this is the only snapshot: re-fetch first, or delete the resource"
                )
            if self.index.pending_jobs(resource_id, "fetch"):
                raise InvalidInput("a fetch is queued or running; delete the snapshot after it")
            resource.snapshots.remove(snapshot)
            if resource.current_snapshot == snapshot.id:
                resource.current_snapshot = resource.snapshots[-1].id
            resource.updated_at = utcnow()
            self.files.write(resource)
            shutil.rmtree(folder, ignore_errors=True)
            chunks, role = self._chunks_for(resource)
            self.index.upsert(resource, chunks, role or "")
        return resource

    async def delete_attachment(self, ref: str, path: str) -> None:
        """Remove one attachment: its file and its entry in ``resource.md``."""
        resource_id = self.resolve(ref)
        async with self.lock(resource_id):
            resource = self.files.read(resource_id)
            attachment = next((a for a in resource.attachments if a.path == path), None)
            if attachment is None:
                raise NotFound(f"no attachment {path!r} on {ref}")
            resource.attachments.remove(attachment)
            resource.updated_at = utcnow()
            self.files.write(
                resource
            )  # file first: an orphan file is harmless, a dangling entry is not
            directory = self.files.attachments_dir(resource_id)
            safe_join(directory, attachment.path).unlink(missing_ok=True)
            if attachment.text_path:
                safe_join(directory, attachment.text_path).unlink(missing_ok=True)
            chunks, role = self._chunks_for(resource)
            self.index.upsert(resource, chunks, role or "")

    async def attach_url(self, ref: str, url: str, added_by: str | None = None) -> str:
        resource_id = self.resolve(ref)
        try:
            detect(url)  # validates scheme and host
        except InvalidURL as exc:
            raise InvalidInput(str(exc)) from exc
        await self._guard(url)
        return self._enqueue("attach_url", resource_id, {"url": url, "added_by": added_by})

    async def reindex(self, ref: str | None = None) -> ReindexReport:
        report = ReindexReport()
        if ref is not None:
            resource_id = self.index.resolve(ref) or ref
            if not is_id(resource_id):
                raise NotFound(ref)
            async with self.lock(resource_id):
                if not self.files.exists(resource_id):
                    self.index.delete(resource_id)
                    return report
                try:
                    resource = self.files.read(resource_id)
                except InvalidResourceFile as exc:
                    report.errors.append(str(exc))
                    return report
                chunks, role = self._chunks_or_error(resource, report.errors)
                self.index.upsert(resource, chunks, role or "")
                report.indexed = 1
            return report

        items: list[tuple[Resource, list[Chunk], str]] = []
        slugs: set[str] = set()
        urls: set[str] = set()
        for resource_id in self.files.resource_ids():
            try:
                resource = self.files.read(resource_id)
            except (InvalidResourceFile, FileNotFoundError) as exc:
                report.errors.append(str(exc))
                continue
            if resource.id != resource_id:
                report.errors.append(f"{resource_id}: id in resource.md is {resource.id}")
                continue
            if resource.slug in slugs or (
                resource.canonical_url and resource.canonical_url in urls
            ):
                report.errors.append(f"{resource_id}: duplicate slug or canonical_url, skipped")
                continue
            slugs.add(resource.slug)
            if resource.canonical_url:
                urls.add(resource.canonical_url)
            for problem in check_meta(resource.kind, resource.meta):
                log.warning("%s: %s", resource_id, problem)
            chunks, role = self._chunks_or_error(resource, report.errors)
            items.append((resource, chunks, role or ""))
        report.indexed = self.index.rebuild(items)
        return report

    # ---------------------------------------------------------------- jobs

    def handlers(self) -> dict[str, JobHandler]:
        return {
            "fetch": self._job_fetch,
            "attach_url": self._job_attach_url,
            "reindex_resource": self._job_reindex,
            "extract_attachment": self._job_extract_attachment,
            "export": self._job_export,
            "import_library": self._job_import_library,
            "import_urls": self._job_import_urls,
        }

    def _enqueue(
        self,
        job_type: str,
        resource_id: str | None,
        payload: dict[str, Any] | None = None,
        run_after: datetime | None = None,
    ) -> str:
        job_id = new_id()
        self.index.enqueue(job_id, job_type, resource_id, payload, run_after)
        return job_id

    def is_final_attempt(self, job: Job, exc: BaseException) -> bool:
        permanent = isinstance(exc, (FetchError, InvalidInput)) and getattr(exc, "permanent", True)
        return permanent or job.attempts >= self.max_attempts

    async def _job_fetch(self, job: Job) -> None:
        assert job.resource_id
        resource_id = job.resource_id
        if not self.files.exists(resource_id):
            raise PermanentFetchError("resource was deleted")
        async with self.lock(resource_id):
            resource = self.files.read(resource_id)
            resource.fetch_status = FetchStatus.FETCHING
            self.files.write(resource)
            self.index.upsert(resource, None)
        staging = self.files.new_staging_dir(job.id)
        try:
            fetcher = self.fetchers.get(resource.kind)
            if fetcher is None:
                raise PermanentFetchError(f"no fetcher for kind {resource.kind}")
            result = await fetcher.fetch(resource, staging)
        except asyncio.CancelledError:
            shutil.rmtree(staging, ignore_errors=True)
            self._fetch_interrupted(resource_id)
            raise
        except BaseException as exc:
            shutil.rmtree(staging, ignore_errors=True)
            await self._record_fetch_failure(resource_id, job, exc)
            raise
        try:
            async with self.lock(resource_id):
                await self._install_snapshot(resource_id, staging, result)
        except asyncio.CancelledError:
            shutil.rmtree(staging, ignore_errors=True)
            self._fetch_interrupted(resource_id)
            raise
        except BaseException as exc:
            shutil.rmtree(staging, ignore_errors=True)
            await self._record_fetch_failure(resource_id, job, exc)
            raise

    async def _install_snapshot(
        self, resource_id: str, staging: Path, result: SnapshotResult
    ) -> None:
        """Move the staged snapshot in and update the resource. Caller holds the lock.

        If anything fails after the move but before resource.md references the snapshot,
        the folder is removed again, so no orphan snapshot is left behind.
        """
        if not self.files.exists(resource_id):
            shutil.rmtree(staging, ignore_errors=True)
            raise PermanentFetchError("resource was deleted during fetch")
        resource = self.files.read(resource_id)  # fresh: a human may have edited it
        first_snapshot = not resource.snapshots
        snapshot_id = self.files.new_snapshot_id(resource_id)
        installed = self.files.install_snapshot(staging, resource_id, snapshot_id)
        try:
            self._apply_snapshot(resource, snapshot_id, result, first_snapshot)
            self.files.write(resource)
        except BaseException:
            shutil.rmtree(installed, ignore_errors=True)
            raise
        chunks, role = self._chunks_for(resource)
        self.index.upsert(resource, chunks, role or "")

    def _apply_snapshot(
        self, resource: Resource, snapshot_id: str, result: SnapshotResult, first_snapshot: bool
    ) -> None:
        resource_id = resource.id
        resource.snapshots.append(Snapshot(id=snapshot_id, artifacts=result.artifacts))
        resource.current_snapshot = snapshot_id
        meta = {k: v for k, v in resource.meta.items() if k not in _SNAPSHOT_META_KEYS}
        meta.update({k: v for k, v in result.meta.items() if v is not None})
        if result.text_role == "content":
            content = resource.artifact("content")
            duplicate = (
                self.index.duplicate_content(resource_id, content.sha256) if content else None
            )
            if duplicate:
                meta["possible_duplicate_of"] = duplicate
        resource.meta = meta
        if (
            result.title
            and resource.title_source != TitleSource.HUMAN
            and _useful_title(result.title)
        ):
            resource.title = result.title.strip()[:300]
            resource.title_source = TitleSource.EXTRACTED
            if first_snapshot and resource.kind == Kind.ARTICLE:
                # The slug was derived from the URL; now we know the real title.
                resource.slug = unique_slug(
                    resource.title, lambda s: self.index.slug_taken(s, resource_id)
                )
        resource.fetch_status = FetchStatus.READY
        resource.fetch_error = None
        resource.updated_at = utcnow()

    def _fetch_interrupted(self, resource_id: str) -> None:
        """Undo FETCHING after a cancel (shutdown, CLI timeout): not a failure, the job is
        re-queued. Synchronous on purpose: a cancelled task should not await, and with no
        await in between nothing else in this process touches the resource meanwhile."""
        try:
            if not self.files.exists(resource_id):
                return
            resource = self.files.read(resource_id)
            if resource.fetch_status != FetchStatus.FETCHING:
                return
            resource.fetch_status = (
                FetchStatus.READY if resource.current_snapshot else FetchStatus.PENDING
            )
            self.files.write(resource)
            self.index.upsert(resource, None)
        except Exception:
            log.exception("could not reset the fetch status of %s", resource_id)

    async def _record_fetch_failure(self, resource_id: str, job: Job, exc: BaseException) -> None:
        try:
            await self._record_fetch_failure_unsafe(resource_id, job, exc)
        except Exception:  # e.g. resource.md made invalid by a hand edit
            log.exception("could not record the fetch failure of %s", resource_id)

    async def _record_fetch_failure_unsafe(
        self, resource_id: str, job: Job, exc: BaseException
    ) -> None:
        if not self.files.exists(resource_id):
            return
        final = self.is_final_attempt(job, exc)
        async with self.lock(resource_id):
            resource = self.files.read(resource_id)
            resource.fetch_error = str(exc) or type(exc).__name__
            resource.fetch_status = (
                FetchStatus.FAILED if final else (
                    FetchStatus.READY if resource.current_snapshot else FetchStatus.PENDING
                )
            )  # fmt: skip
            if final and resource.current_snapshot:
                # A failed re-fetch leaves the previous snapshot in place and usable.
                resource.fetch_status = FetchStatus.READY
            resource.updated_at = utcnow()
            self.files.write(resource)
            self.index.upsert(resource, None)

    async def _job_attach_url(self, job: Job) -> None:
        assert job.resource_id
        url = job.payload["url"]
        if not self.files.exists(job.resource_id):
            raise PermanentFetchError("resource was deleted")
        staging = self.files.new_staging_dir(job.id)
        try:
            git = self.fetchers.get(Kind.GIT_REPO)
            if _is_repository(url) and isinstance(git, GitRepoFetcher):
                # A repository page (github.com/owner/repo/...) or a .git URL: attach a
                # bare clone of the project rather than the HTML of the page.
                repo = await git.archive(url, staging)
                await self._store_attachment(
                    job.resource_id,
                    repo.path,
                    repo.path.name,
                    "application/gzip",
                    url,
                    job.payload.get("added_by"),
                )
                return
            downloaded = await download_attachment(url, staging, self.settings)
            await self._store_attachment(
                job.resource_id,
                downloaded.path,
                downloaded.filename,
                downloaded.mime,
                url,
                job.payload.get("added_by"),
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    async def _job_reindex(self, job: Job) -> None:
        await self.reindex(job.resource_id)

    async def _store_attachment(
        self,
        resource_id: str,
        source: Path,
        filename: str,
        mime: str | None,
        origin_url: str | None,
        added_by: str | None,
    ) -> Attachment:
        sha = await asyncio.to_thread(sha256_file, source)  # up to attachments.max_mb
        size = source.stat().st_size
        async with self.lock(resource_id):
            resource = self.files.read(resource_id)
            directory = self.files.attachments_dir(resource_id)
            name = unique_name(directory, sanitize_filename(filename))
            target = safe_join(directory, name)
            shutil.move(source, target)
            attachment = Attachment(
                path=name,
                mime=(mime if mime and mime != "application/octet-stream" else None)
                or guess_mime(name),
                size=size,
                sha256=sha,
                origin_url=origin_url,
                added_at=utcnow(),
                added_by=added_by,
            )
            resource.attachments.append(attachment)
            resource.updated_at = utcnow()
            self.files.write(resource)
            self.index.upsert(resource, None)
        if detect_format(target, name, attachment.mime) is not None:
            self._enqueue("extract_attachment", resource_id, {"path": name})
        return attachment

    def extract_attachment(self, ref: str, path: str) -> str:
        """Queue (again) the text extraction of one attachment."""
        resource = self.get(ref)
        if not any(a.path == path for a in resource.attachments):
            raise NotFound(f"no attachment {path!r} on {ref}")
        return self._enqueue("extract_attachment", resource.id, {"path": path})

    async def _job_extract_attachment(self, job: Job) -> None:
        assert job.resource_id
        resource_id, name = job.resource_id, str(job.payload["path"])
        if not self.files.exists(resource_id):
            raise PermanentFetchError("resource was deleted")
        directory = self.files.attachments_dir(resource_id)
        attachments = self.files.read(resource_id).attachments
        current = next((a for a in attachments if a.path == name), None)
        if current is None:
            raise PermanentFetchError(f"attachment {name} was deleted")
        staging = self.files.new_staging_dir(job.id)
        try:
            result = await extract_to(
                safe_join(directory, name), staging, name, current.mime,
                limits=self.settings.extract,
            )  # fmt: skip
            async with self.lock(resource_id):
                resource = self.files.read(resource_id)
                attachment = next((a for a in resource.attachments if a.path == name), None)
                if attachment is None or attachment.sha256 != current.sha256:
                    return  # deleted or replaced meanwhile
                content = staging / "content.md"
                if content.exists():
                    text_dir = directory / ATTACHMENT_TEXT_DIR
                    text_dir.mkdir(exist_ok=True)
                    os.replace(content, text_dir / f"{name}.md")
                    attachment.text_path = f"{ATTACHMENT_TEXT_DIR}/{name}.md"
                else:
                    attachment.text_path = None
                attachment.extraction = result.meta.get("extraction")
                if (
                    attachment.text_path
                    and resource.title_source != TitleSource.HUMAN
                    and (resource.title_source == TitleSource.URL or _weak(resource))
                ):
                    # The page gave no real title (a bot check says "Reddit"): a PDF saved
                    # from the browser carries the page's title, in metadata or filename.
                    title = result.title if result.title and _useful_title(result.title) else None
                    resource.title = (title or _title_from_filename(name)).strip()[:300]
                    resource.title_source = TitleSource.EXTRACTED
                resource.updated_at = utcnow()
                self.files.write(resource)
                chunks, role = self._chunks_for(resource)
                self.index.upsert(resource, chunks, role or "")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ------------------------------------------------------- export / import (ADR-0015)

    def exports(self) -> list[ExportInfo]:
        root = self.settings.exports_dir
        if not root.is_dir():
            return []
        found = []
        for entry in sorted(root.iterdir(), reverse=True):
            if entry.is_dir() and EXPORT_ID_RE.match(entry.name):
                info = ExportInfo.load(entry)
                if info is not None:
                    found.append(info)
        return found

    def _transfer_jobs(self, job_type: str) -> list[Job]:
        active = self.index.jobs("queued", limit=1000) + self.index.jobs("running", limit=1000)
        return [j for j in active if j.type == job_type]

    def start_export(self) -> ExportInfo:
        """Queue a full export into ``<data>/exports/<id>/``. One at a time."""
        if self._transfer_jobs("export"):
            raise InvalidInput("an export is already queued or running")
        export_id = utcnow().strftime(SNAPSHOT_FORMAT)
        directory = self.settings.exports_dir / export_id
        if directory.exists():
            raise InvalidInput("an export was started this second; try again")
        directory.mkdir(parents=True)
        info = ExportInfo(id=export_id, part_mb=self.settings.export.part_mb)
        info.save(directory)
        self._enqueue("export", None, {"export_id": export_id})
        return info

    async def export_library(self, export_id: str, directory: Path | None = None) -> ExportInfo:
        """Write every resource into tar parts. Each resource is copied under its lock."""
        if not EXPORT_ID_RE.match(export_id):
            raise InvalidInput(f"not an export id: {export_id!r}")
        directory = directory or self.settings.exports_dir / export_id
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob(f"alexandria-{export_id}-*.tar"):  # a retried export
            old.unlink()
        part_mb = self.settings.export.part_mb
        info = ExportInfo(id=export_id, part_mb=part_mb)
        info.save(directory)
        writer = ExportWriter(directory, info, part_mb * 1_000_000)
        try:
            for resource_id in list(self.files.resource_ids()):
                async with self.lock(resource_id):
                    if self.files.exists(resource_id):
                        resource_dir = self.files.resource_dir(resource_id)
                        await asyncio.to_thread(writer.add_resource, resource_dir)
            await asyncio.to_thread(writer.close)
        except BaseException as exc:
            writer.abort()
            info.status, info.error = "failed", str(exc) or type(exc).__name__
            info.finished_at = iso(utcnow())
            info.save(directory)
            raise
        info.status, info.finished_at = "done", iso(utcnow())
        info.save(directory)
        return info

    def export_part(self, export_id: str, name: str) -> Path:
        info = self._export(export_id)
        if name not in {p.name for p in info.parts} or info.status != "done":
            raise NotFound(f"no part {name} in export {export_id}")
        return self.settings.exports_dir / export_id / name

    def delete_export(self, export_id: str) -> None:
        info = self._export(export_id)
        if info.status == "running" and self._transfer_jobs("export"):
            raise InvalidInput("this export is still running")
        shutil.rmtree(self.settings.exports_dir / export_id)

    def _export(self, export_id: str) -> ExportInfo:
        info = None
        if EXPORT_ID_RE.match(export_id):
            info = ExportInfo.load(self.settings.exports_dir / export_id)
        if info is None:
            raise NotFound(f"no export {export_id}")
        return info

    async def receive_import(self, filename: str, chunks: AsyncIterator[bytes]) -> str:
        """Store an uploaded export part in ``<data>/imports/`` and queue its import."""
        root = self.settings.imports_dir
        root.mkdir(parents=True, exist_ok=True)
        name = f"{new_id()}-{sanitize_filename(filename, 'export.tar')}"
        target = root / name
        limit = self.settings.export.max_upload_mb * 1_000_000
        size = 0
        try:
            with target.open("wb") as fh:
                async for block in chunks:
                    size += len(block)
                    if size > limit:
                        raise InvalidInput(f"file exceeds {self.settings.export.max_upload_mb} MB")
                    fh.write(block)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        if not size:
            target.unlink()
            raise InvalidInput(f"{filename} is empty")
        return self._enqueue("import_library", None, {"file": name, "name": filename})

    def import_reports(self, limit: int = 10) -> list[dict[str, Any]]:
        root = self.settings.imports_dir
        if not root.is_dir():
            return []
        reports = []
        for path in sorted(root.glob("*.report.json"), reverse=True)[:limit]:
            try:
                reports.append(json.loads(path.read_text(encoding="utf-8")))
            except OSError, ValueError:
                continue
        return reports

    async def import_library(self, archive: Path, name: str | None = None) -> LibraryImportReport:
        """Add every resource of one export part. Resources already here are left alone."""
        report = LibraryImportReport(file=name or archive.name)
        staging = self.files.new_staging_dir(f"import-{new_id()}")
        try:
            try:
                manifest, ids, problems = await asyncio.to_thread(unpack, archive, staging)
            except ArchiveError as exc:
                raise InvalidInput(f"{report.file}: {exc}") from exc
            report.export_id, report.part = manifest.get("export_id"), manifest.get("part")
            report.errors.extend(problems)
            for resource_id in ids:
                try:
                    await self._import_resource(resource_id, staging / resource_id, report)
                except (InvalidResourceFile, OSError, ValueError) as exc:
                    report.errors.append(f"{resource_id}: {exc}")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        report.finished_at = iso(utcnow())
        return report

    async def _import_resource(
        self, resource_id: str, source: Path, report: LibraryImportReport
    ) -> None:
        resource_md = source / RESOURCE_FILE
        if not resource_md.is_file():
            report.errors.append(f"{resource_id}: no {RESOURCE_FILE}, skipped")
            return
        resource = parse_resource(
            resource_md.read_text(encoding="utf-8"), source=f"{resource_id}/{RESOURCE_FILE}"
        )
        if resource.id != resource_id:
            report.errors.append(f"{resource_id}: id in {RESOURCE_FILE} is {resource.id}")
            return
        refetch = False
        async with self.lock(resource_id):
            if self.files.exists(resource_id):
                report.existing.append(resource_id)
                return
            url = resource.canonical_url
            other = self.index.find_by_canonical_url(url) if url else None
            if other and other != resource_id:
                report.duplicates.append(f"{resource_id} ({url}, already {other})")
                return
            changed = False
            if self.index.slug_taken(resource.slug, resource_id):
                resource.slug = unique_slug(resource.slug, self.index.slug_taken)
                changed = True
            if resource.fetch_status in (FetchStatus.PENDING, FetchStatus.FETCHING):
                # Its queued fetch stayed in the other installation's database.
                resource.fetch_status, refetch, changed = FetchStatus.PENDING, True, True
            target = self.files.resource_dir(resource_id)
            if target.exists():  # a folder without resource.md: debris
                shutil.rmtree(target)
            os.replace(source, target)
            if changed:
                self.files.write(resource)
            chunks, role = self._chunks_or_error(resource, report.errors)
            self.index.upsert(resource, chunks, role or "")
        if refetch and resource.kind != Kind.NOTE:
            self._enqueue("fetch", resource_id)
        report.imported.append(resource_id)

    async def _job_export(self, job: Job) -> None:
        await self.export_library(str(job.payload["export_id"]))

    async def _job_import_library(self, job: Job) -> None:
        name = sanitize_filename(str(job.payload.get("file", "")))
        archive = self.settings.imports_dir / name
        if not archive.is_file():
            raise InvalidInput(f"uploaded file {name} is gone")
        report_path = self.settings.imports_dir / f"{name}.report.json"
        try:
            report = await self.import_library(archive, str(job.payload.get("name") or name))
        except InvalidInput as exc:
            failed = LibraryImportReport(file=str(job.payload.get("name") or name))
            failed.errors.append(str(exc))
            failed.finished_at = iso(utcnow())
            _write_json(report_path, failed)
            archive.unlink(missing_ok=True)
            raise
        _write_json(report_path, report)
        archive.unlink(missing_ok=True)

    # ---------------------------------------------------------------- helpers

    async def _guard(self, url: str) -> None:
        """Reject private hosts up front. The fetcher checks again on every hop."""
        try:
            await check_url(url, self.settings.security.allow_private_hosts)
        except BlockedURL as exc:
            raise InvalidInput(f"blocked by SSRF guard: {exc}") from exc

    def _chunks_for(self, resource: Resource) -> tuple[list[Chunk], str | None]:
        """Chunks of the main text, then of every attachment's extracted text."""
        text, role = self._main_text(resource)
        chunks = chunk_markdown(text) if text.strip() else []
        for attachment in resource.attachments:
            extra = self._read_attachment_text(resource.id, attachment)
            if not extra or not extra.strip():
                continue
            for c in chunk_markdown(extra):
                heading = attachment.path
                if c.heading_path:
                    heading = f"{heading} > {c.heading_path}"
                chunks.append(Chunk(len(chunks), heading, c.text, role="attachment"))
        return chunks, role

    def _chunks_or_error(
        self, resource: Resource, errors: list[str]
    ) -> tuple[list[Chunk], str | None]:
        """``_chunks_for``, but a hand-edited or damaged file (a bad snapshot id, a path out
        of the folder, text that is not UTF-8) indexes the resource without its text and
        reports it, instead of failing a whole reindex or import."""
        try:
            return self._chunks_for(resource)
        except (ValueError, OSError) as exc:
            errors.append(f"{resource.id}: indexed without its text: {exc}")
            return [], None

    def startup(self) -> int:
        """Reset interrupted jobs and wipe staging. Returns how many jobs were reset."""
        self.files.wipe_tmp()
        return self.index.reset_running_jobs()

    def stats(self) -> dict[str, Any]:
        return {"by_status": self.index.counts(), "now": iso(utcnow())}


def _weak(resource: Resource) -> bool:
    """The fetched content is missing or too thin to stand for the resource."""
    return resource.kind != Kind.NOTE and (
        resource.meta.get("extraction") in ("poor", "failed", "unsupported")
        or not resource.current_snapshot
    )


def _is_repository(url: str) -> bool:
    try:
        return detect(url).kind == Kind.GIT_REPO
    except InvalidURL:
        return False


def _write_json(path: Path, report: LibraryImportReport) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _scope(scope: Scope | None, project: str | None) -> tuple[Scope, str | None]:
    project = (project or "").strip() or None
    if scope is None:
        scope = Scope.PROJECT if project else Scope.GLOBAL
    if scope == Scope.PROJECT and not project:
        raise InvalidInput("scope 'project' needs a project name")
    if scope == Scope.GLOBAL:
        project = None if project is None else project
    return scope, project


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")


def _capped_entries(text: str, report: ImportReport) -> list[tuple[str, list[str], str | None]]:
    entries = parse_url_list(text)
    if len(entries) > MAX_IMPORT:
        report.errors.append(f"only the first {MAX_IMPORT} of {len(entries)} links imported")
        entries = entries[:MAX_IMPORT]
    return entries


def parse_url_list(text: str) -> list[tuple[str, list[str], str | None]]:
    """(url, tags, title) from a bookmarks HTML export or any text containing URLs."""
    found: dict[str, tuple[str, list[str], str | None]] = {}
    if re.search(r"<a\s[^>]*href=", text, flags=re.IGNORECASE):
        import lxml.html

        try:
            tree = lxml.html.fromstring(text)
        except ValueError, lxml.etree.ParserError:
            tree = None
        for a in tree.iter("a") if tree is not None else []:
            href = (a.get("href") or "").strip()
            if href.lower().startswith(("http://", "https://")):
                tags = [t for t in (a.get("tags") or "").split(",") if t.strip()]
                title = " ".join(a.text_content().split()) or None
                found.setdefault(href, (href, tags, title))
    for match in _URL_IN_TEXT.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}")
        found.setdefault(url, (url, [], None))
    return list(found.values())


def _useful_title(title: str) -> bool:
    """PDF and Office metadata titles are often junk ("Microsoft Word - doc1.docx")."""
    clean = title.strip().lower()
    return len(clean) >= 3 and not clean.startswith(("microsoft word -", "untitled"))


def _title_from_filename(filename: str) -> str:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    words = " ".join(stem.replace("_", " ").replace("-", " ").split())
    return words[:200] or "Untitled document"


def _title_from_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.strip("/")
    last = path.rsplit("/", 1)[-1] if path else ""
    last = last.rsplit(".", 1)[0] if "." in last else last
    words = last.replace("-", " ").replace("_", " ").strip()
    host = parts.hostname or url
    return f"{words} ({host})" if words else host
