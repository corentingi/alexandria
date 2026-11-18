"""Domain models. ``Resource`` mirrors the frontmatter of ``resource.md`` (plus its body)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)


class Kind(StrEnum):
    ARTICLE = "article"
    GIT_REPO = "git_repo"
    NOTE = "note"
    DOCUMENT = "document"  # an uploaded file (PDF, DOCX, EPUB, text…), kept in source/
    MEDIA = "media"  # a video or audio page, fetched with yt-dlp (ADR-0021)


class Status(StrEnum):
    INBOX = "inbox"
    KEPT = "kept"
    ARCHIVED = "archived"


class FetchStatus(StrEnum):
    PENDING = "pending"
    FETCHING = "fetching"
    READY = "ready"
    FAILED = "failed"


class Scope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"


class TitleSource(StrEnum):
    """Where the title came from. Only a ``human`` title is protected from extraction."""

    URL = "url"
    EXTRACTED = "extracted"
    HUMAN = "human"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class MediaLevel(StrEnum):
    """What a media fetch keeps besides details, captions and thumbnail (ADR-0021)."""

    TRANSCRIPT = "transcript"  # captions only: no audio or video file
    AUDIO = "audio"
    VIDEO = "video"  # picture and sound in one file


VIDEO_HEIGHTS = (360, 480, 720, 1080, 1440, 2160)


class MediaChoice(BaseModel):
    """A human's choice for a media resource; a fetcher only reads it."""

    model_config = ConfigDict(extra="forbid")

    keep: MediaLevel = MediaLevel.TRANSCRIPT
    max_height: int | None = None  # video only; None: the server default

    @field_validator("max_height")
    @classmethod
    def _known_height(cls, value: int | None) -> int | None:
        if value is not None and value not in VIDEO_HEIGHTS:
            raise ValueError(f"max_height must be one of {', '.join(map(str, VIDEO_HEIGHTS))}")
        return value


class Artifact(BaseModel):
    """A file inside a snapshot. Role-specific keys (``history``, ``head_commit``…) are kept."""

    model_config = ConfigDict(extra="allow")

    role: str
    path: str
    mime: str
    size: int
    sha256: str


class Snapshot(BaseModel):
    id: str
    artifacts: list[Artifact] = Field(default_factory=list)


class Attachment(BaseModel):
    path: str
    mime: str
    size: int
    sha256: str
    origin_url: str | None = None
    added_at: datetime
    added_by: str | None = None
    # Text extracted from the file (PDF, DOCX…), relative to attachments/ (ADR-0017).
    text_path: str | None = None
    extraction: str | None = None  # ok | poor | failed | unsupported


class Resource(BaseModel):
    """Strict core; ``meta`` is open and validated softly per kind (see ``check_meta``)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    slug: str
    kind: Kind
    title: str
    title_source: TitleSource = TitleSource.URL
    source_url: str | None = None
    canonical_url: str | None = None
    why: str | None = None
    tags: list[str] = Field(default_factory=list)
    scope: Scope = Scope.GLOBAL
    project: str | None = None
    status: Status = Status.INBOX
    fetch_status: FetchStatus = FetchStatus.PENDING
    fetch_error: str | None = None
    captured_at: datetime
    updated_at: datetime
    created_by: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    current_snapshot: str | None = None
    snapshots: list[Snapshot] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    # For documents: the uploaded original, stored in source/ (ADR-0012).
    source_file: Attachment | None = None
    # For media: what to keep (transcript, audio or video), chosen by a human.
    media: MediaChoice | None = None
    generated: dict[str, Any] = Field(default_factory=dict)
    # The Markdown body of resource.md: human notes (for a note, the note itself).
    notes: str = Field(default="", exclude=True)

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str]) -> list[str]:
        return normalize_tags(value)

    @field_validator("notes")
    @classmethod
    def _strip_notes(cls, value: str) -> str:
        # resource.md bodies come back stripped; strip on the way in too, so live chunks
        # and reindexed chunks are identical (acceptance criterion 6).
        return value.strip()

    @field_validator("why", "project")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    def snapshot(self, snapshot_id: str | None = None) -> Snapshot | None:
        wanted = snapshot_id or self.current_snapshot
        return next((s for s in self.snapshots if s.id == wanted), None)

    def artifact(self, role: str, snapshot_id: str | None = None) -> Artifact | None:
        snap = self.snapshot(snapshot_id)
        if snap is None:
            return None
        return next((a for a in snap.artifacts if a.role == role), None)


def normalize_tags(tags: list[str]) -> list[str]:
    """Lowercase, trimmed, spaces to dashes, deduplicated, order kept."""
    seen: dict[str, None] = {}
    for tag in tags:
        clean = "-".join(tag.strip().lower().split())
        if clean:
            seen.setdefault(clean, None)
    return list(seen)


class ArticleMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    author: str | None = None
    published_at: str | None = None
    sitename: str | None = None
    language: str | None = None
    content_type: str | None = None
    possible_duplicate_of: str | None = None
    extraction: str | None = None


class DocumentMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    format: str | None = None
    extraction: str | None = None
    pages: int | None = None
    author: str | None = None


class GitRepoMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    forge: str | None = None
    description: str | None = None
    default_branch: str | None = None
    license: str | None = None
    topics: list[str] = Field(default_factory=list)
    stars: int | None = None
    size_kb: int | None = None
    language: str | None = None
    archived: bool | None = None
    pushed_at: str | None = None
    archive_skipped: str | None = None
    lfs: bool | None = None


class MediaMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    site: str | None = None
    video_id: str | None = None
    channel: str | None = None
    channel_url: str | None = None
    duration_s: int | None = None
    published_at: str | None = None
    language: str | None = None
    captions: str | None = None  # manual | automatic | none
    media_kept: str | None = None  # audio | video, when a file was saved
    media_height: int | None = None  # the resolution: shorter side (1080 for 1080x1920)
    media_skipped: str | None = None  # why the file asked for was not saved


_META_SCHEMAS: dict[Kind, type[BaseModel]] = {
    Kind.ARTICLE: ArticleMeta,
    Kind.GIT_REPO: GitRepoMeta,
    Kind.DOCUMENT: DocumentMeta,
    Kind.MEDIA: MediaMeta,
}


def check_meta(kind: Kind, meta: dict[str, Any]) -> list[str]:
    """Soft validation: returns problems, never raises, never drops keys."""
    schema = _META_SCHEMAS.get(kind)
    if schema is None:
        return []
    try:
        schema.model_validate(meta)
    except ValidationError as exc:
        return [f"meta.{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
    return []
