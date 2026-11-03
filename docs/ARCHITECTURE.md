# Architecture

How the parts fit. The original design is in [DESIGN.md](DESIGN.md); where the code
departs from it, an ADR in [adrs/](adrs/README.md) says why.

## One process

```
uvicorn ── app.Root ──┬── /mcp ──► McpAuth ──► MCP streamable HTTP (mcp_server.py)
                      └── else ──► FastAPI ──┬── /api/v1/*  (api/routes.py)
                                             ├── /*         (ui/routes.py, Jinja2 + htmx)
                                             └── lifespan: Worker.start(), MCP session manager
```

Every interface (REST, MCP, UI, CLI) calls **`library.Library`**, the only place that
changes data. `views.py` holds the JSON shapes shared by REST and MCP.

## Write path

1. `Library.ingest` canonicalizes the URL (`urls.py`), rejects private hosts
   (`security.check_url`), checks `canonical_url` for a duplicate, writes `resource.md`
   (`storage/files.py`), indexes it (`storage/index.py`) and enqueues a `fetch` job.
2. `jobs.Worker` (N asyncio tasks in the server process) claims the job with an atomic
   `UPDATE … RETURNING`, and calls `Library._job_fetch`.
3. The kind's fetcher (`fetchers/article.py`, `fetchers/git_repo.py`) works in
   `tmp/<job-id>/` and returns a `SnapshotResult`.
4. Under the resource's asyncio lock, the staging dir is renamed to
   `snapshots/<timestamp>/`, `resource.md` is re-read (a human may have edited it),
   updated and rewritten, then chunks and FTS rows are rebuilt.
5. Failures: `FetchError(permanent=…)`. Permanent ones (404, SSRF, size cap) fail at once;
   others retry with backoff (`worker.backoff_s`) up to `worker.max_attempts`.

The CLI (`alexandria add`) writes the same files and the same queue; a running server's
worker picks the job up (same SQLite file, WAL mode). `--now` runs it in-process.

Documents (ADR-0012): `Library.ingest_file` stores the upload in `source/`, keyed by its
SHA-256 (`canonical_url = urn:sha256:…`), and enqueues the same `fetch` job; for a
document, the fetcher extracts text from `source/` into a new snapshot.

## Read path

`Index.search` queries `resources_fts` (title ×5, why ×3, tags ×3, notes ×2, meta text ×1)
and `chunks_fts` (×1), sums the best bm25 of each per resource, then applies filters in
SQL. Empty query = listing by `captured_at`. Snippets come from the best chunk, with
`\x02`/`\x03` markers that each interface turns into `**…**` or `<mark>`.

Content (`get_content`) is read from the current snapshot's `content.md` or `README.md`,
or the note body, paginated by chunk or offset.

## On disk

See DESIGN.md §5. Additions: `tokens.yaml` (ADR-0003), `session_secret`, and the
`title_source` field in `resource.md` (ADR-0008).

## Modules

| Module | Role |
| --- | --- |
| `config.py` | Settings: env `ALEXANDRIA_*` (nested with `__`) over `<data>/config.yaml` |
| `models.py` | Pydantic `Resource`, `Snapshot`, `Artifact`, `Attachment`, enums, soft meta schemas |
| `ids.py`, `urls.py` | UUIDv7, slugs; canonicalization and kind detection |
| `security.py` | SSRF guard (swappable resolver), filename sanitizing, `safe_join` |
| `storage/files.py` | Folder layout, atomic writes, frontmatter (de)serialization |
| `storage/index.py` | SQLite schema, upserts, rebuild, search, job queue |
| `chunking.py` | Deterministic Markdown chunking |
| `extract.py` | Text extraction: PDF (pypdf), DOCX, EPUB, HTML, Markdown, text (ADR-0012) |
| `fetchers/` | `base.download` (guarded streaming GET), article, git, document, attachment |
| `library.py` | All operations + job handlers |
| `jobs.py` | Worker loop, retries |
| `walls.py` | Known login/bot/paywall sites and bot-check detection |
| `transfer.py` | Export parts (tar writer, size split) and safe unpacking for import |
| `auth.py` | Token store and scopes |
| `api/`, `mcp_server.py`, `ui/`, `cli.py` | Interfaces |
| `app.py` | ASGI composition, bootstrap token |
