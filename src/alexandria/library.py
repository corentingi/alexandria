"""The library: every operation the interfaces expose, in one place.

Writes always go to ``resource.md`` first (the source of truth), then to the index. A
per-resource asyncio lock serializes edits to one resource (API PATCH vs. the worker).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from alexandria.chunking import Chunk, chunk_markdown
from alexandria.config import Settings
from alexandria.fetchers.article import ArticleFetcher
from alexandria.fetchers.attachment import download_attachment
from alexandria.fetchers.base import Fetcher, FetchError, PermanentFetchError
from alexandria.fetchers.document import DocumentFetcher
from alexandria.fetchers.git_repo import GitRepoFetcher
from alexandria.ids import new_id, unique_slug
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
from alexandria.storage.files import FileStore, InvalidResourceFile, guess_mime, sha256_file
from alexandria.storage.index import Index, Job, SearchFilters, SearchHit
from alexandria.urls import InvalidURL, detect

log = logging.getLogger(__name__)

# Meta keys that describe one snapshot; cleared before a new snapshot's meta is merged.
_SNAPSHOT_META_KEYS = {
    "extraction", "archive_skipped", "possible_duplicate_of", "final_url", "lfs",
    "content_type",
}  # fmt: skip

MAX_CONTENT_CHARS = 50_000


class NotFound(LookupError):
    pass


class InvalidInput(ValueError):
    pass


@dataclass
class IngestResult:
    resource: Resource
    duplicate: bool
    job_id: str | None = None


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
        """Full text of the current content and the role it came from."""
        if resource.kind == Kind.NOTE:
            return resource.notes, "note"
        for role in ("content", "readme"):
            text = self.files.read_artifact_text(resource, role)
            if text is not None:
                return text, role
        return "", None

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
            if existing is not None:
                return IngestResult(self.files.read(existing), duplicate=True)
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
            sha = sha256_file(tmp)
            canonical = f"urn:sha256:{sha}"
            scope, project = _scope(scope, project)
            async with self._ingest_lock:
                existing = self.index.find_by_canonical_url(canonical)
                if existing is not None:
                    return IngestResult(self.files.read(existing), duplicate=True)
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
        """Ingest every link in a plain list or a browser bookmarks export."""
        report = ImportReport()
        entries = parse_url_list(text)
        if len(entries) > MAX_IMPORT:
            report.errors.append(f"only the first {MAX_IMPORT} of {len(entries)} links imported")
            entries = entries[:MAX_IMPORT]
        for url, extra_tags, title in entries:
            try:
                result = await self.ingest(
                    url,
                    why=why,
                    tags=[*(tags or []), *extra_tags],
                    project=project,
                    title=title,
                    created_by=created_by,
                )
            except InvalidInput as exc:
                report.errors.append(f"{url}: {exc}")
                continue
            target = report.duplicates if result.duplicate else report.created
            target.append(result.resource.id)
        return report

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
            self.index.upsert(resource, chunk_markdown(body), "note")
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
                self.index.upsert(resource, chunk_markdown(resource.notes), "note")
            else:
                self.index.upsert(resource, None)
        return resource

    async def delete(self, ref: str) -> str:
        resource_id = self.resolve(ref)
        async with self.lock(resource_id):
            self.files.delete(resource_id)
            self.index.delete(resource_id)
        self._locks.pop(resource_id, None)
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
            async with self.lock(resource_id):
                if not self.files.exists(resource_id):
                    self.index.delete(resource_id)
                    return report
                try:
                    resource = self.files.read(resource_id)
                except InvalidResourceFile as exc:
                    report.errors.append(str(exc))
                    return report
                chunks, role = self._chunks_for(resource)
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
            chunks, role = self._chunks_for(resource)
            items.append((resource, chunks, role or ""))
        report.indexed = self.index.rebuild(items)
        return report

    # ---------------------------------------------------------------- jobs

    def handlers(self) -> dict[str, JobHandler]:
        return {
            "fetch": self._job_fetch,
            "attach_url": self._job_attach_url,
            "reindex_resource": self._job_reindex,
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
        except BaseException as exc:
            shutil.rmtree(staging, ignore_errors=True)
            await self._record_fetch_failure(resource_id, job, exc)
            raise
        async with self.lock(resource_id):
            if not self.files.exists(resource_id):
                shutil.rmtree(staging, ignore_errors=True)
                raise PermanentFetchError("resource was deleted during fetch")
            resource = self.files.read(resource_id)  # fresh: a human may have edited it
            first_snapshot = not resource.snapshots
            snapshot_id = self.files.new_snapshot_id(resource_id)
            self.files.install_snapshot(staging, resource_id, snapshot_id)
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
                if first_snapshot and resource.kind in (Kind.ARTICLE, Kind.DOCUMENT):
                    # The slug was derived from the URL; now we know the real title.
                    resource.slug = unique_slug(
                        resource.title, lambda s: self.index.slug_taken(s, resource_id)
                    )
            resource.fetch_status = FetchStatus.READY
            resource.fetch_error = None
            resource.updated_at = utcnow()
            self.files.write(resource)
            chunks, role = self._chunks_for(resource)
            self.index.upsert(resource, chunks, role or "")

    async def _record_fetch_failure(self, resource_id: str, job: Job, exc: BaseException) -> None:
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
        sha = sha256_file(source)
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
        return attachment

    # ---------------------------------------------------------------- helpers

    async def _guard(self, url: str) -> None:
        """Reject private hosts up front. The fetcher checks again on every hop."""
        try:
            await check_url(url, self.settings.security.allow_private_hosts)
        except BlockedURL as exc:
            raise InvalidInput(f"blocked by SSRF guard: {exc}") from exc

    def _chunks_for(self, resource: Resource) -> tuple[list[Chunk], str | None]:
        text, role = self.text(resource)
        return (chunk_markdown(text) if text.strip() else []), role

    def startup(self) -> int:
        """Reset interrupted jobs and wipe staging. Returns how many jobs were reset."""
        self.files.wipe_tmp()
        return self.index.reset_running_jobs()

    def stats(self) -> dict[str, Any]:
        return {"by_status": self.index.counts(), "now": iso(utcnow())}


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
