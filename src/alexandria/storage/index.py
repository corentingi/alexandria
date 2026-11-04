"""SQLite index and job queue (design §7, §8.7, §9).

The index is derived data: every table but ``jobs`` is rebuilt from ``resource.md`` files
by ``reindex``. One writer connection, guarded by a lock; reads open their own connection.
Calls are short and synchronous; async callers run them inline (they are local and fast).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Collection, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from alexandria.chunking import Chunk
from alexandria.models import Resource, iso, utcnow

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT,
    canonical_url TEXT UNIQUE,
    why TEXT,
    notes TEXT,
    status TEXT NOT NULL,
    fetch_status TEXT NOT NULL,
    fetch_error TEXT,
    scope TEXT NOT NULL,
    project TEXT,
    captured_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT,
    meta JSON NOT NULL DEFAULT '{}',
    current_snapshot TEXT
);
CREATE INDEX IF NOT EXISTS resources_captured ON resources(captured_at DESC);

CREATE TABLE IF NOT EXISTS tags (
    resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (resource_id, tag)
);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS artifacts (
    resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    snapshot TEXT,              -- NULL = attachment
    role TEXT NOT NULL,
    path TEXT NOT NULL,
    mime TEXT,
    size INTEGER,
    sha256 TEXT,
    origin_url TEXT,
    created_at TEXT,
    meta JSON NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS artifacts_sha ON artifacts(role, sha256);
CREATE INDEX IF NOT EXISTS artifacts_resource ON artifacts(resource_id);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    snapshot TEXT,
    ord INTEGER NOT NULL,
    heading_path TEXT NOT NULL,
    source_role TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_resource ON chunks(resource_id, ord);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading_path, content='chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, heading_path) VALUES (new.id, new.text, new.heading_path);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, heading_path)
    VALUES ('delete', old.id, old.text, old.heading_path);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS resources_fts USING fts5(
    resource_id UNINDEXED, title, why, notes, tags, meta_text,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    resource_id TEXT,
    type TEXT NOT NULL,
    status TEXT NOT NULL,       -- queued | running | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after TEXT NOT NULL,
    payload JSON NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status, run_after);
"""

# Index tables rebuilt by reindex (everything but jobs). Order matters for FKs.
_DERIVED_TABLES = ("chunks", "artifacts", "tags", "resources")

# Text in meta worth searching (descriptions, topics, authors…).
_META_TEXT_KEYS = ("description", "topics", "author", "sitename", "language", "forge")

# FTS5 snippet markers, replaced by each interface (``**`` for agents, <mark> in the UI).
HL_START, HL_END = "\x02", "\x03"


@dataclass
class Job:
    id: str
    resource_id: str | None
    type: str
    status: str
    attempts: int
    run_after: str
    payload: dict[str, Any]
    error: str | None
    created_at: str
    updated_at: str


@dataclass
class SearchFilters:
    kind: str | None = None
    tags: list[str] = field(default_factory=list)
    project: str | None = None
    scope: str | None = None
    status: str | None = None
    captured_after: str | None = None
    captured_before: str | None = None


@dataclass
class SearchHit:
    id: str
    score: float
    chunk_id: int | None = None
    chunk_ord: int | None = None
    heading_path: str | None = None
    snippet: str | None = None


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word must match, as a prefix."""
    words = re.findall(r"\w+", text, flags=re.UNICODE)
    if not words:
        return None
    return " ".join(f'"{w}"*' for w in words)


class Index:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = self._connect()
        self._writer.executescript(_SCHEMA)
        self._writer.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._writer.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def close(self) -> None:
        self._writer.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """The single writer: one transaction, serialized across threads and tasks."""
        with self._lock:
            try:
                self._writer.execute("BEGIN IMMEDIATE")
                yield self._writer
                self._writer.commit()
            except BaseException:
                self._writer.rollback()
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # -- resources ---------------------------------------------------------------

    def upsert(
        self, resource: Resource, chunks: Sequence[Chunk] | None, source_role: str = ""
    ) -> None:
        """Index a resource. ``chunks=None`` keeps the existing chunks."""
        with self.write() as db:
            _upsert(db, resource, chunks, source_role)

    def delete(self, resource_id: str) -> None:
        with self.write() as db:
            _delete(db, resource_id)

    def rebuild(self, items: Iterable[tuple[Resource, Sequence[Chunk], str]]) -> int:
        """Replace every derived table in one transaction. Jobs are untouched."""
        count = 0
        with self.write() as db:
            for table in _DERIVED_TABLES:
                db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names
            db.execute("DELETE FROM resources_fts")
            db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            for resource, chunks, role in items:
                _upsert(db, resource, chunks, role)
                count += 1
        return count

    def resolve(self, ref: str) -> str | None:
        """An id or a slug → id."""
        with self.read() as db:
            row = db.execute(
                "SELECT id FROM resources WHERE id = ? OR slug = ?", (ref, ref)
            ).fetchone()
        return row["id"] if row else None

    def find_by_canonical_url(self, url: str) -> str | None:
        with self.read() as db:
            row = db.execute("SELECT id FROM resources WHERE canonical_url = ?", (url,)).fetchone()
        return row["id"] if row else None

    def slug_taken(self, slug: str, except_id: str | None = None) -> bool:
        with self.read() as db:
            row = db.execute(
                "SELECT 1 FROM resources WHERE slug = ? AND id IS NOT ?", (slug, except_id)
            ).fetchone()
        return row is not None

    def row(self, resource_id: str) -> dict[str, Any] | None:
        with self.read() as db:
            row = db.execute("SELECT * FROM resources WHERE id = ?", (resource_id,)).fetchone()
            if row is None:
                return None
            tags = [
                r["tag"]
                for r in db.execute(
                    "SELECT tag FROM tags WHERE resource_id = ? ORDER BY rowid", (resource_id,)
                )
            ]
        return _row_dict(row) | {"tags": tags}

    def rows(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self.read() as db:
            rows = db.execute(f"SELECT * FROM resources WHERE id IN ({marks})", ids).fetchall()  # noqa: S608
            tag_rows = db.execute(
                f"SELECT resource_id, tag FROM tags WHERE resource_id IN ({marks}) ORDER BY rowid",  # noqa: S608
                ids,
            ).fetchall()
        tags: dict[str, list[str]] = {}
        for t in tag_rows:
            tags.setdefault(t["resource_id"], []).append(t["tag"])
        return {r["id"]: _row_dict(r) | {"tags": tags.get(r["id"], [])} for r in rows}

    def chunks(self, resource_id: str) -> list[dict[str, Any]]:
        with self.read() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id, ord, heading_path, source_role, length(text) AS chars, text "
                    "FROM chunks WHERE resource_id = ? ORDER BY ord",
                    (resource_id,),
                )
            ]

    def duplicate_content(self, resource_id: str, sha256: str) -> str | None:
        """Another resource whose *current* content has this hash."""
        with self.read() as db:
            row = db.execute(
                "SELECT a.resource_id FROM artifacts a JOIN resources r ON r.id = a.resource_id "
                "WHERE a.role = 'content' AND a.sha256 = ? AND a.snapshot = r.current_snapshot "
                "AND a.resource_id != ? ORDER BY r.captured_at LIMIT 1",
                (sha256, resource_id),
            ).fetchone()
        return row["resource_id"] if row else None

    def list_tags(self) -> list[tuple[str, int]]:
        with self.read() as db:
            return [
                (r["tag"], r["n"])
                for r in db.execute(
                    "SELECT tag, count(*) AS n FROM tags GROUP BY tag ORDER BY n DESC, tag"
                )
            ]

    def counts(self) -> dict[str, int]:
        with self.read() as db:
            rows = db.execute("SELECT status, count(*) AS n FROM resources GROUP BY status")
            return {r["status"]: r["n"] for r in rows}

    # -- search ------------------------------------------------------------------

    def search(
        self,
        query: str,
        filters: SearchFilters,
        limit: int = 10,
        offset: int = 0,
    ) -> list[SearchHit]:
        where, params = _filter_sql(filters)
        match = fts_query(query)
        with self.read() as db:
            if match is None:
                rows = db.execute(
                    f"SELECT r.id FROM resources r WHERE {where} "  # noqa: S608
                    "ORDER BY r.captured_at DESC, r.id DESC LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
                return [SearchHit(id=r["id"], score=0.0) for r in rows]

            scores: dict[str, float] = {}
            for r in db.execute(
                # Column weights: resource_id, title, why, notes, tags, meta_text.
                "SELECT resource_id, bm25(resources_fts, 0, 5.0, 3.0, 2.0, 3.0, 1.0) AS s "
                "FROM resources_fts WHERE resources_fts MATCH ?",
                (match,),
            ):
                scores[r["resource_id"]] = r["s"]

            best: dict[str, sqlite3.Row] = {}
            for r in db.execute(
                "SELECT c.resource_id, c.id, c.ord, c.heading_path, bm25(chunks_fts) AS s, "
                "snippet(chunks_fts, 0, char(2), char(3), '…', 24) AS snip "  # = HL_START, HL_END
                "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? ORDER BY s",
                (match,),
            ):
                best.setdefault(r["resource_id"], r)

            candidates = set(scores) | set(best)
            if not candidates:
                return []
            marks = ",".join("?" * len(candidates))
            allowed = {
                r["id"]
                for r in db.execute(
                    f"SELECT r.id FROM resources r WHERE r.id IN ({marks}) AND {where}",  # noqa: S608
                    [*candidates, *params],
                )
            }
        hits = []
        for rid in allowed:
            chunk = best.get(rid)
            hits.append(
                SearchHit(
                    id=rid,
                    score=scores.get(rid, 0.0) + (chunk["s"] if chunk else 0.0),
                    chunk_id=chunk["id"] if chunk else None,
                    chunk_ord=chunk["ord"] if chunk else None,
                    heading_path=chunk["heading_path"] if chunk else None,
                    snippet=chunk["snip"] if chunk else None,
                )
            )
        hits.sort(key=lambda h: (h.score, h.id))
        return hits[offset : offset + limit]

    # -- jobs --------------------------------------------------------------------

    def enqueue(
        self,
        job_id: str,
        job_type: str,
        resource_id: str | None,
        payload: dict[str, Any] | None = None,
        run_after: datetime | None = None,
    ) -> None:
        now = iso(utcnow())
        with self.write() as db:
            db.execute(
                "INSERT INTO jobs (id, resource_id, type, status, attempts, run_after, payload, "
                "created_at, updated_at) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)",
                (
                    job_id,
                    resource_id,
                    job_type,
                    iso(run_after) if run_after else now,
                    json.dumps(payload or {}),
                    now,
                    now,
                ),
            )

    def claim_job(self, only: Collection[str] | None = None) -> Job | None:
        """Claim the next due job; with ``only``, the next due job among these ids."""
        now = iso(utcnow())
        among, params = "", [now, now]
        if only is not None:
            if not only:
                return None
            among = f"AND id IN ({','.join('?' * len(only))}) "
            params = [now, now, *only]
        with self.write() as db:
            row = db.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? "  # noqa: S608 - only ? marks
                "WHERE id = (SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ? "
                f"{among}ORDER BY run_after, created_at LIMIT 1) RETURNING *",
                params,
            ).fetchone()
        return _job(row) if row else None

    def requeue_job(self, job_id: str) -> None:
        """Put an interrupted job back in the queue; the interruption is not an attempt."""
        with self.write() as db:
            db.execute(
                "UPDATE jobs SET status = 'queued', attempts = max(attempts - 1, 0), "
                "updated_at = ? WHERE id = ? AND status = 'running'",
                (iso(utcnow()), job_id),
            )

    def finish_job(self, job_id: str) -> None:
        with self.write() as db:
            db.execute(
                "UPDATE jobs SET status = 'done', error = NULL, updated_at = ? WHERE id = ?",
                (iso(utcnow()), job_id),
            )

    def fail_job(self, job_id: str, error: str, retry_at: datetime | None) -> None:
        with self.write() as db:
            if retry_at is None:
                db.execute(
                    "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                    (error, iso(utcnow()), job_id),
                )
            else:
                db.execute(
                    "UPDATE jobs SET status = 'queued', error = ?, run_after = ?, updated_at = ? "
                    "WHERE id = ?",
                    (error, iso(retry_at), iso(utcnow()), job_id),
                )

    def retry_job(self, job_id: str) -> Job | None:
        now = iso(utcnow())
        with self.write() as db:
            row = db.execute(
                "UPDATE jobs SET status = 'queued', attempts = 0, run_after = ?, updated_at = ? "
                "WHERE id = ? AND status = 'failed' RETURNING *",
                (now, now, job_id),
            ).fetchone()
        return _job(row) if row else None

    def reset_running_jobs(self) -> int:
        with self.write() as db:
            cur = db.execute(
                "UPDATE jobs SET status = 'queued', updated_at = ? WHERE status = 'running'",
                (iso(utcnow()),),
            )
            return cur.rowcount

    def job(self, job_id: str) -> Job | None:
        with self.read() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    def jobs(
        self, status: str | None = None, resource_id: str | None = None, limit: int = 100
    ) -> list[Job]:
        clauses, params = ["1 = 1"], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if resource_id:
            clauses.append("resource_id = ?")
            params.append(resource_id)
        with self.read() as db:
            rows = db.execute(
                f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} "  # noqa: S608
                "ORDER BY created_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [_job(r) for r in rows]

    def pending_jobs(self, resource_id: str, job_type: str) -> int:
        with self.read() as db:
            row = db.execute(
                "SELECT count(*) AS n FROM jobs WHERE resource_id = ? AND type = ? "
                "AND status IN ('queued', 'running')",
                (resource_id, job_type),
            ).fetchone()
        return int(row["n"])

    def backup(self, dest: Path) -> None:
        with self.read() as db:
            db.execute("VACUUM INTO ?", (str(dest),))


def _upsert(
    db: sqlite3.Connection, r: Resource, chunks: Sequence[Chunk] | None, source_role: str
) -> None:
    db.execute(
        "INSERT INTO resources (id, slug, kind, title, source_url, canonical_url, why, notes, "
        "status, fetch_status, fetch_error, scope, project, captured_at, updated_at, created_by, "
        "meta, current_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET slug=excluded.slug, kind=excluded.kind, "
        "title=excluded.title, source_url=excluded.source_url, "
        "canonical_url=excluded.canonical_url, why=excluded.why, notes=excluded.notes, "
        "status=excluded.status, fetch_status=excluded.fetch_status, "
        "fetch_error=excluded.fetch_error, scope=excluded.scope, project=excluded.project, "
        "captured_at=excluded.captured_at, updated_at=excluded.updated_at, "
        "created_by=excluded.created_by, meta=excluded.meta, "
        "current_snapshot=excluded.current_snapshot",
        (
            r.id,
            r.slug,
            r.kind.value,
            r.title,
            r.source_url,
            r.canonical_url,
            r.why,
            r.notes,
            r.status.value,
            r.fetch_status.value,
            r.fetch_error,
            r.scope.value,
            r.project,
            iso(r.captured_at),
            iso(r.updated_at),
            r.created_by,
            json.dumps(r.meta, default=str),
            r.current_snapshot,
        ),
    )
    db.execute("DELETE FROM tags WHERE resource_id = ?", (r.id,))
    db.executemany("INSERT INTO tags (resource_id, tag) VALUES (?, ?)", [(r.id, t) for t in r.tags])

    db.execute("DELETE FROM artifacts WHERE resource_id = ?", (r.id,))
    for snap in r.snapshots:
        for a in snap.artifacts:
            extra = a.model_dump(exclude={"role", "path", "mime", "size", "sha256"})
            db.execute(
                "INSERT INTO artifacts (resource_id, snapshot, role, path, mime, size, sha256, "
                "created_at, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                (r.id, snap.id, a.role, a.path, a.mime, a.size, a.sha256, snap.id,
                 json.dumps(extra, default=str)),
            )  # fmt: skip
    for att in r.attachments:
        db.execute(
            "INSERT INTO artifacts (resource_id, snapshot, role, path, mime, size, sha256, "
            "origin_url, created_at, meta) VALUES (?,NULL,'attachment',?,?,?,?,?,?,?)",
            (r.id, att.path, att.mime, att.size, att.sha256, att.origin_url, iso(att.added_at),
             json.dumps({"added_by": att.added_by})),
        )  # fmt: skip

    db.execute("DELETE FROM resources_fts WHERE resource_id = ?", (r.id,))
    db.execute(
        "INSERT INTO resources_fts (resource_id, title, why, notes, tags, meta_text) "
        "VALUES (?,?,?,?,?,?)",
        # A note's body is its content: it is searched through chunks, not as notes.
        (
            r.id,
            r.title,
            r.why or "",
            "" if r.kind.value == "note" else r.notes,
            " ".join(r.tags),
            _meta_text(r.meta),
        ),
    )

    if chunks is not None:
        db.execute("DELETE FROM chunks WHERE resource_id = ?", (r.id,))
        db.executemany(
            "INSERT INTO chunks (resource_id, snapshot, ord, heading_path, source_role, text) "
            "VALUES (?,?,?,?,?,?)",
            [(r.id, r.current_snapshot, c.ord, c.heading_path, c.role or source_role, c.text)
             for c in chunks],
        )  # fmt: skip


def _delete(db: sqlite3.Connection, resource_id: str) -> None:
    db.execute("DELETE FROM resources_fts WHERE resource_id = ?", (resource_id,))
    for table in _DERIVED_TABLES[:-1]:
        db.execute(f"DELETE FROM {table} WHERE resource_id = ?", (resource_id,))  # noqa: S608
    db.execute("DELETE FROM resources WHERE id = ?", (resource_id,))


def _meta_text(meta: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in _META_TEXT_KEYS:
        value = meta.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif value:
            parts.append(str(value))
    return " ".join(parts)


def _filter_sql(f: SearchFilters) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if f.kind:
        clauses.append("r.kind = ?")
        params.append(f.kind)
    if f.project:
        clauses.append("r.project = ?")
        params.append(f.project)
    if f.scope:
        clauses.append("r.scope = ?")
        params.append(f.scope)
    if f.status:
        clauses.append("r.status = ?")
        params.append(f.status)
    else:
        clauses.append("r.status != 'archived'")
    if f.captured_after:
        clauses.append("r.captured_at >= ?")
        params.append(f.captured_after)
    if f.captured_before:
        clauses.append("r.captured_at < ?")
        params.append(f.captured_before)
    for tag in f.tags:
        clauses.append("EXISTS (SELECT 1 FROM tags t WHERE t.resource_id = r.id AND t.tag = ?)")
        params.append(tag)
    return " AND ".join(clauses) or "1 = 1", params


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["meta"] = json.loads(data.get("meta") or "{}")
    return data


def _job(row: sqlite3.Row) -> Job:
    data = dict(row)
    data["payload"] = json.loads(data.get("payload") or "{}")
    return Job(**data)
