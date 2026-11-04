# Alexandria — Design Document

> The original v1 design, kept as written. Where the implementation departs from it, an ADR
> in [adrs/](adrs/README.md) records the change and why.

## 1. Pitch

Alexandria is a self-hosted personal knowledge base shared by a human and their AI agents.
You (or an agent) give it a URL. It fetches, archives and indexes the content, and records
*why* it was saved. Agents read and write through MCP. The human uses a small web UI.
It runs as a single container with a single volume, designed to live on a home NAS.

The name refers to the Library of Alexandria, which copied the books found on ships
arriving in its port.

## 2. Principles

1. **Files are the source of truth.** Each resource is a folder on disk containing a
   Markdown file and its artifacts. The SQLite database is an index that can be rebuilt
   from the files at any time. The only thing lost in a rebuild is the job queue.
2. **Deterministic code for mechanical work.** Fetching, extraction, cloning, archiving,
   hashing, deduplication and indexing are plain code. LLMs are only used where
   judgment on language adds value (summaries, tag suggestions). They are never in the
   fetch/archive path, and the system is fully functional without them.
3. **v1 contains no LLM integration.** Section 15 reserves the extension points, but v1
   does not implement them.
4. **"Why" is a first-class field.** A resource without a human-written reason stays in
   the inbox until one is added.
5. **Human-written and machine-generated data are always distinguishable.** Human
   fields (`why`, `notes`, `tags`) are never overwritten by machine output.
6. **One process, one container, one volume.** No Redis, no external database, no
   separate worker container.
7. **Fetched content is untrusted.** It is stored and served as data and never
   interpreted as instructions.

## 3. Scope

### v1 includes
- Resource kinds: `article` (any web URL), `git_repo` (GitHub enrichment; plain
  HTTPS git URLs also work), `note` (free text with no URL).
- Attachments on any resource (uploaded file, or file downloaded from a URL).
- Re-fetch, which creates a new snapshot and keeps the old ones.
- Full-text search (SQLite FTS5) with filters.
- MCP server, REST API, web UI (with bookmarklet), CLI.
- Scoped bearer tokens (`read`, `write`, `admin`).

### Non-goals for v1
LLMs, embeddings and vector search; headless-browser rendering of JavaScript-heavy pages;
PDF text extraction; scheduled re-fetch; metadata enrichment for forges other than GitHub;
multi-user support; public exposure or OAuth; browser extension; filesystem watcher.

## 4. Concepts

- **Resource**: the single entity type. It has a fixed core of fields plus an open
  `meta` object for kind-specific data.
- **Snapshot**: one fetch of a resource's source, stored in a timestamped folder and
  never modified afterwards. Re-fetching adds a new snapshot. `current_snapshot` points
  to the latest successful one.
- **Artifact**: a file inside a snapshot (raw HTML, extracted Markdown, repository
  archive, README…).
- **Attachment**: a file added to a resource by a human or an agent, independent of
  snapshots.
- **Chunk**: a section of text content (extracted article, README, note body) used for
  search snippets and partial reads by agents. Chunks are derived data and can always
  be rebuilt.

### Identifiers
- `id`: a UUIDv7 (sortable by time). It is immutable and is the folder name on disk.
  Use `uuid.uuid7()` on Python 3.14+, otherwise the `uuid6` package.
- `slug`: human- and agent-friendly, unique, derived from the title (`owner-repo` for
  GitHub repos, with `-2`, `-3`… on collision). It is stored in the frontmatter and the
  index and **never used in filesystem paths**, so renaming a resource never moves
  files.
- Every interface accepts a `ref`, which is either an `id` or a `slug`.

## 5. Storage layout

```
/data
  alexandria.db                    # index + job queue (WAL mode)
  config.yaml                      # optional; env vars take precedence
  tmp/                             # per-job staging dirs, wiped at startup
  resources/
    0192f3a4-7c1e-7b2a-9d3e-5f6a7b8c9d0e/
      resource.md                  # frontmatter + human notes (body)
      snapshots/
        20261102T143000Z/
          page.html                # article: raw response body
          content.md               # article: extracted main content
          fetch.json               # request/response metadata
        20261215T090000Z/
          ...
      attachments/
        release-v1.2.zip
```

Git repository snapshot:
```
snapshots/20261102T143000Z/
  repository.git.tar.gz            # gzipped tar of a bare clone
  README.md
  github.json                      # GitHub API repo metadata (GitHub only)
  fetch.json
```

Rules:
- A snapshot is built in `/data/tmp/<job-id>/`, then moved into place with
  `os.rename`. The move is atomic because `tmp/` and `resources/` are on the same
  filesystem.
- `resource.md` is always written atomically: write `resource.md.tmp`, then
  `os.replace`.
- Deleting a resource means deleting its folder and its index rows.
- Each resource folder is self-contained. Copying one folder copies everything about
  that resource.

## 6. Resource file format (`resource.md`)

```yaml
---
id: 0192f3a4-7c1e-7b2a-9d3e-5f6a7b8c9d0e
slug: owner-repo
kind: git_repo                       # article | git_repo | note
title: owner/repo
source_url: https://github.com/Owner/Repo/
canonical_url: https://github.com/owner/repo
why: "Alternative to X for Y; check before building our own"
tags: [self-hosted, mcp]
scope: global                        # global | project
project: null
status: kept                         # inbox | kept | archived
fetch_status: ready                  # pending | fetching | ready | failed
fetch_error: null
captured_at: 2026-11-02T14:30:00Z
updated_at: 2026-11-02T14:31:12Z
created_by: claude-code              # token name
meta:                                # open, kind-specific
  forge: github
  description: "..."
  default_branch: main
  license: MIT
  topics: [...]
current_snapshot: 20261102T143000Z
snapshots:
  - id: 20261102T143000Z
    artifacts:
      - role: repository
        path: repository.git.tar.gz
        mime: application/gzip
        size: 48213004
        sha256: "..."
        history: full                # full | shallow
        depth: null                  # e.g. 1000 when shallow
        head_commit: 3f2a9c1...
        commit_count: 2841
        default_branch: main
      - role: readme
        path: README.md
        mime: text/markdown
        size: 8123
        sha256: "..."
attachments:
  - path: release-v1.2.zip
    mime: application/zip
    size: 1048576
    sha256: "..."
    origin_url: https://github.com/owner/repo/releases/download/v1.2/release.zip
    added_at: 2026-11-02T15:00:00Z
    added_by: corentin
generated: {}                        # reserved for LLM output (v2); always empty in v1
---
Free-form human notes in Markdown.
```

- Validated with Pydantic models: a strict model for the core fields and a soft schema
  per kind for `meta`. Unknown `meta` keys are kept, never rejected.
- Article `meta` holds deterministic extraction output: `author`, `published_at`,
  `sitename`, `language`, `content_type`, `possible_duplicate_of`.
- `title` is filled from extraction unless a human has set it.

### Status rules
- A resource created **with** a `why` gets `status: kept`; **without** one it gets
  `status: inbox`.
- Adding a `why` to an inbox resource moves it to `kept`.
- `archived` resources are excluded from search by default.

## 7. Index (SQLite)

WAL mode, `busy_timeout` set, and **a single writer connection**: all writes go through
one connection guarded by an asyncio lock. Reads use separate connections.

```sql
resources(id TEXT PK, slug TEXT UNIQUE, kind, title, source_url,
          canonical_url TEXT UNIQUE, why, notes, status, fetch_status,
          scope, project, captured_at, updated_at, created_by,
          meta JSON, current_snapshot)
tags(resource_id, tag, PRIMARY KEY(resource_id, tag))
artifacts(resource_id, snapshot, role, path, mime, size, sha256,
          origin_url, created_at, meta JSON)          -- snapshot NULL = attachment
chunks(id INTEGER PK, resource_id, snapshot, ord, heading_path, source_role, text)
chunks_fts    USING fts5(text, heading_path, content='chunks', content_rowid='id',
                         tokenize='unicode61 remove_diacritics 2')
resources_fts USING fts5(title, why, notes, tags, meta_text,
                         tokenize='unicode61 remove_diacritics 2')
jobs(id TEXT PK, resource_id, type, status, attempts, run_after,
     payload JSON, error, created_at, updated_at)
```

**Chunking:** split Markdown on headings, then split long sections on paragraph
boundaries into chunks of at most ~2,000 characters. Keep the heading path (`H1 > H2`).
Sources: `content.md` (articles), `README.md` (repos), the note body (notes). Chunk
boundaries must be stable so v2 can reuse the same chunks for embeddings.

**Reindex** (`alexandria reindex [ref]`): scan `resources/*/resource.md`, validate, and
rebuild every table except `jobs`. Report invalid files without aborting. Acceptance
test: delete the DB, reindex, and get identical search results.

## 8. Ingestion workflow

```
ingest(url, why?, tags?, scope?, project?)
  → canonicalize URL, detect kind
  → duplicate check on canonical_url
      exists → return existing resource {duplicate: true}; never overwrite
  → create folder + resource.md (fetch_status: pending), index it
  → enqueue job "fetch"
  → return {id, slug, status} immediately (HTTP 202)

worker:
  claim job → fetch_status: fetching
  → run kind-specific fetcher in /data/tmp/<job-id>/
  → move snapshot into resources/<id>/snapshots/<ts>/
  → update resource.md (title, meta, snapshots, current_snapshot), atomically
  → rebuild chunks + FTS for the resource
  → fetch_status: ready (or failed + fetch_error)
```

### 8.1 URL canonicalization and kind detection
- Canonicalization: lowercase scheme and host; drop the fragment and default ports;
  remove tracking parameters (`utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `ref`);
  sort the remaining query parameters; remove the trailing slash except on the root.
- `https://github.com/<owner>/<repo>[/...|.git]` → `git_repo` with `meta.forge: github`.
  Canonical form: `https://github.com/<owner>/<repo>`, lowercase.
- Any other URL ending in `.git`, or an explicit `kind: git_repo` → generic `git_repo`.
- Everything else → `article`.

### 8.2 Article fetcher
1. Streaming `httpx` GET: timeout 30 s, at most 5 redirects, 20 MB byte cap,
   identifying User-Agent (`Alexandria/<version>`). Apply the SSRF guard (section 12) on
   every redirect hop.
2. Save the raw body as `page.html`. Write `fetch.json` (final URL, status code,
   content-type, selected headers, timings).
3. If the content is HTML: run `trafilatura` to produce `content.md` (Markdown output)
   and metadata (title, author, date, sitename, language).
   If the extracted text is under ~500 characters, set `meta.extraction: poor`.
   A future headless-browser fallback will use this flag.
4. If the content is not HTML (PDF, image, zip…): save it as `original.<ext>`, set
   `meta.content_type`, and skip extraction in v1.
5. Hash `content.md`. If it matches another resource's current content, set
   `meta.possible_duplicate_of: <id>`. This is informational only.

### 8.3 Git repository fetcher
Goal: keep the full history whenever possible, otherwise at least the last 1,000
commits, stored as a single `.tar.gz` file.

1. **GitHub only:** `GET /repos/{owner}/{repo}` → `github.json`. Copy description,
   topics, license, stars, default branch, `size` (KB), primary language, `archived`
   and `pushed_at` into `meta`. Fetch the README via `/repos/{owner}/{repo}/readme`
   (raw) → `README.md`. An optional `GITHUB_TOKEN` raises rate limits and allows private
   repositories.
2. **Clone strategy:**
   - If the known size is at most `git.full_history_max_mb` (default 500), or the size
     is unknown: `git clone --bare <url>` (all branches and tags, full history).
   - If the size is above the threshold, or the full clone exceeds
     `git.clone_timeout` (default 15 min) or fails for size reasons:
     `git clone --bare --depth <git.shallow_depth> --single-branch` (default 1000,
     default branch only).
   - Do **not** use `--mirror`: on GitHub it also fetches `refs/pull/*`, which inflates
     the size.
   - If the result still exceeds `git.max_archive_mb` (default 2000): skip the archive,
     keep the metadata and README, and set `meta.archive_skipped: too_large`.
3. Record `head_commit` (`git rev-parse HEAD`), `commit_count`
   (`git rev-list --count HEAD`), `history` (full/shallow) and `depth`.
4. Package with Python's `tarfile` (`w:gz`) → `repository.git.tar.gz`. Git packfiles are
   already zlib-compressed, so the default compression level is enough.
   **Why tar.gz:** a single file that works identically for full and shallow clones,
   preserves the bare repository's layout and the shallow marker, and is the native
   format on Linux.
5. Restore procedure (also documented in the README):
   `tar -xzf repository.git.tar.gz && git clone repository.git my-checkout`.
6. Git LFS objects and submodules are not archived. Record this in `meta` if
   `.gitattributes` references LFS.
7. Generic (non-GitHub) git URLs follow steps 2–6. They have no API metadata, and the
   README is read from the bare repository's default branch if present.

### 8.4 Notes
`create_note(title, body, why?, tags?, ...)` creates a `kind: note` resource. The body
goes in the `resource.md` body. There is no fetch job; it is chunked and indexed
immediately.

### 8.5 Attachments
- Upload: multipart via the API or UI. Filename is sanitized: basename only, a
  whitelist of characters, a numeric suffix on collision, and the resolved path must
  stay inside `attachments/`.
- From URL: `attach_url(ref, url)` enqueues an `attach_url` job, using the same
  streaming download, SSRF guard and size cap (`attachments.max_mb`, default 500).
- Recorded with `sha256`, `size`, `mime`, `origin_url`, `added_by`.

### 8.6 Re-fetch
`refetch(ref)` enqueues a new fetch that creates a new snapshot. Previous snapshots are
kept. Chunks and FTS are rebuilt from the new current snapshot. Incremental `git fetch`
into the previous bare repository is on the roadmap.

### 8.7 Jobs and failures
- Job types in v1: `fetch`, `attach_url`, `reindex_resource`.
- The worker is an asyncio loop in the same process. Jobs are claimed with
  `UPDATE ... WHERE status='queued' AND run_after <= now ... RETURNING`.
  Concurrency is configurable (default 2, since jobs are network-bound). Only the
  database writes are serialized. Subprocesses (`git`) run through
  `asyncio.create_subprocess_exec`.
- Retries: 3 attempts with backoff (1 min, 5 min, 30 min). Permanent errors (404, 410,
  invalid URL, blocked by the SSRF guard, over the size cap) fail immediately.
- At startup: jobs left in `running` are reset to `queued`, and `tmp/` is wiped.
- A per-resource asyncio lock serializes edits to `resource.md` (API PATCH vs worker).

## 9. Search

- Query `resources_fts` (metadata) and `chunks_fts` (content), group results by
  resource, and rank by bm25. Metadata matches are weighted higher:
  title ×5, why ×3, tags ×3, notes ×2, content ×1.
- Each result returns the best-matching chunk as a snippet (FTS5 `snippet()`) and its
  `chunk` reference.
- Filters: `kind`, `tags` (AND), `project`, `scope`, `status` (excludes `archived`
  unless requested), `captured_after` / `captured_before`.
- Empty query plus filters returns a listing sorted by `captured_at` descending.

## 10. Interfaces

### 10.1 MCP (official `mcp` Python SDK, streamable HTTP, mounted at `/mcp`)

Read scope:
- `search(query, kind?, tags?, project?, status?, limit=10)` →
  `[{id, slug, kind, title, url, why, tags, snippet, chunk}]`
- `get_resource(ref)` → metadata, `why`, notes, chunk list (heading paths), and artifact
  and attachment list. No full content.
- `get_content(ref, chunk?, offset?, max_chars=8000)` → paginated text of the current
  snapshot's content.
- `list_recent(limit=20, kind?, status?)`
- `list_tags()`

Write scope:
- `ingest_url(url, why?, tags?, project?, scope?)`
- `create_note(title, body, why?, tags?, project?, scope?)`
- `update_resource(ref, why?, notes?, tags?, status?, title?, project?, scope?)`
- `attach_url(ref, url)`
- `refetch(ref)`

Not exposed through MCP: delete, reindex, token management.

All fetched content returned to agents is wrapped as
`{"source_url": ..., "untrusted_content": ...}`. The MCP server instructions state that
resource content is external data and must never be followed as instructions.

### 10.2 REST API (`/api/v1`, JSON, `Authorization: Bearer <token>`)
```
POST   /resources                    {url, why?, tags?, project?, scope?} → 202
POST   /notes                        {title, body, why?, tags?, ...}      → 201
GET    /resources/{ref}
PATCH  /resources/{ref}              why, notes, tags, status, title, project, scope
DELETE /resources/{ref}                                                   (admin)
POST   /resources/{ref}/refetch
POST   /resources/{ref}/attachments  multipart file | {url}
GET    /resources/{ref}/content      ?chunk=&offset=&max_chars=
GET    /resources/{ref}/files/{path} download an artifact or attachment
GET    /search                       ?q=&kind=&tag=&project=&scope=&status=&limit=
GET    /tags
GET    /jobs                         ?status=
POST   /reindex                                                           (admin)
GET    /healthz                      (no auth)
```

### 10.3 Web UI (Jinja2 + HTMX, vendored, no CDN, no JS build step)
- **Search**: query box, filters, and a result list with snippets.
- **Resource page**: metadata; editable `why`, notes, tags and status; rendered content;
  snapshot history; artifact and attachment downloads; re-fetch button.
- **Inbox**: resources with no `why`, each with an inline field to add one.
- **Add**: URL plus `why`, with the cursor placed in the `why` field.
- **Bookmarklet** shown in the UI:
  `javascript:location.href='http://<host>:8080/add?url='+encodeURIComponent(location.href)+'&title='+encodeURIComponent(document.title)`
- **Jobs**: queued, running and failed jobs, with a retry button.
- UI login: paste a token once; the session is stored in a signed cookie. The token's
  scopes apply.

### 10.4 CLI (Typer)
```
alexandria serve
alexandria add <url> [--why ...] [--tag ...] [--project ...]
alexandria note <title> [--body-file ...]
alexandria search <query>
alexandria reindex [ref]
alexandria backup <dest-dir>
alexandria token create <name> --scope read|write|admin   # prints token once, stores hash
alexandria token list | revoke <name>
```

## 11. Auth (v1)
- Static bearer tokens stored as SHA-256 hashes in the database (managed via the CLI),
  compared in constant time.
- Scopes: `read` < `write` < `admin` (each includes the previous ones).
  Typical setup: read-only tokens for most agents, a write token for ingesting agents,
  and admin for the human.
- `created_by` / `added_by` record the token name.
- The service is meant for the LAN only. TLS and OAuth are out of scope for v1.

## 12. Security
- **SSRF guard** for every outbound HTTP request: resolve DNS and reject loopback,
  RFC 1918, link-local, CGNAT (100.64/10), multicast, IPv6 ULA/link-local and metadata
  addresses, unless the host is in `security.allow_private_hosts`. Re-check on every
  redirect. Articles allow only `http`/`https`.
- **Git:** set `GIT_ALLOW_PROTOCOL=https` (no `file://`, `ssh`, `ext::`) and
  `GIT_TERMINAL_PROMPT=0`. Clones are bare (nothing checked out, no hooks run) and
  submodules are not fetched. Apply the SSRF guard to the host before cloning.
- **Paths:** never build filesystem paths from user input, except sanitized attachment
  names. Always resolve the path and verify it stays inside the resource folder.
- **Size caps and timeouts** on every download and on clones. Downloads are streamed and
  aborted when the cap is exceeded.
- **Untrusted content** is labelled as such in MCP output (section 10.1).
- **Destructive operations** (delete, reindex) require the admin scope and are not
  available through MCP.
- The container runs as a non-root user.

## 13. Configuration
Environment variables (`ALEXANDRIA_*`), optionally overridden by `/data/config.yaml`.
Managed with `pydantic-settings`.
```yaml
data_dir: /data
http: { host: 0.0.0.0, port: 8080, session_secret: <random> }
fetch: { timeout_s: 30, max_redirects: 5, max_bytes: 20000000, user_agent: "Alexandria/0.1", min_bytes_per_s: 50000 }
git: { full_history_max_mb: 500, shallow_depth: 1000, clone_timeout_s: 900, max_archive_mb: 2000 }
github: { token: null }
attachments: { max_mb: 500 }
worker: { concurrency: 2, max_attempts: 3 }
security: { allow_private_hosts: [] }
```

## 14. Tech stack and project layout
- Python 3.13+ (3.14 for stdlib `uuid7`), Poetry, `src/` layout, strict type hints, ruff.
- FastAPI + uvicorn; official `mcp` SDK mounted in the same app; Jinja2 + HTMX.
- `sqlite3` (stdlib, FTS5); `httpx`; `trafilatura`; `python-frontmatter`; Pydantic v2 +
  `pydantic-settings`; Typer; the `git` CLI; `tarfile` (stdlib).
- Tests: pytest, pytest-asyncio, respx.

## 15. Roadmap

### v2: LLM enrichment (optional; the system works without it)
- LLM calls are routed by **task**, not by provider. Tasks: `summarize`, `suggest_tags`,
  `embed`. Config maps each task to a provider, model and prompt version.
- Providers: `anthropic` (official SDK) and `openai_compat` (`base_url` + key).
- New jobs `enrich` and `embed` are queued after a successful fetch. An LLM failure never
  blocks or fails a resource.
- Output goes under `generated` with `provider`, `model`, `prompt_version` and `at`.
  Suggested tags stay in `generated.tags` and are never merged into human `tags`
  automatically; the UI offers one-click accept.
- Output is validated with Pydantic, with one retry that includes the validation error.
- Embeddings use `sqlite-vec`, keyed by `(chunk_id, model)`. Search becomes hybrid
  (FTS5 bm25 + vector KNN) with reciprocal rank fusion. Changing the model triggers a
  re-embed job.

### v3 and later
Headless-browser fallback (Playwright) for pages flagged `extraction: poor`;
single-file HTML archives (monolith); image capture; PDF text extraction; scheduled
incremental `git fetch` into the previous bare repository; GitLab and other forge
metadata; relations between resources; browser extension; integration with the
"control tower" project.

## 16. Deployment
- Dockerfile based on `python:3.13-slim`, plus `git` and `ca-certificates`. Multi-arch
  build (amd64 and arm64). Non-root user, `VOLUME /data`, `EXPOSE 8080`, `HEALTHCHECK`
  on `/healthz`, entrypoint `alexandria serve`.
- The volume **must be local storage on the host** (no NFS or SMB mount), because SQLite
  WAL mode requires working file locking and shared memory.

## 17. Backup and restore
- `resources/` contains plain files. Snapshots never change and `resource.md` writes are
  atomic, so rsync or NAS snapshots are safe.
- Database: `alexandria backup` runs `VACUUM INTO`. Alternatively, rebuild it with
  `alexandria reindex`; only the job queue is lost.

## 18. Acceptance criteria (v1)
1. `docker compose up` with an empty volume gives a working UI on port 8080.
2. Adding an article URL returns an id immediately. Within seconds the resource is
   `ready` with `page.html`, `content.md` and `fetch.json`, and is findable by search.
3. Adding a small GitHub repository produces `repository.git.tar.gz`. Extracting it and
   running `git clone` yields every branch, every tag and the full commit count.
   A repository above the size threshold falls back to depth 1000, and the frontmatter
   records `history: shallow`.
4. A resource added without `why` appears in the inbox. Adding a `why` moves it to
   `kept`.
5. Ingesting a URL that is already stored returns the existing resource with
   `duplicate: true`.
6. Deleting `alexandria.db` and running `alexandria reindex` gives identical search
   results.
7. A `read` token can search and read but cannot ingest. A `write` token can ingest but
   cannot delete.
8. Ingesting `http://192.168.1.1/` or `http://localhost/` is rejected by the SSRF guard.
9. Re-fetching a resource creates a second snapshot and keeps the first.
10. The unit test suite runs without network access.
