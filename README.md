# Alexandria

A self-hosted personal knowledge base shared by a human and their AI agents. Give it a URL
and a reason; it fetches, archives and indexes the content, and remembers **why** you kept
it. Agents read and write through MCP, you use a small web UI. One container, one volume.

- **Articles** — raw HTML, extracted Markdown, metadata, full-text search.
- **Git repositories** — a bare clone (full history, or the last 1,000 commits for huge
  repos) packed as `repository.git.tar.gz`, plus GitHub metadata and README.
- **Notes** — Markdown, searchable at once.
- **Files are the truth** — every resource is a folder with a `resource.md`; the SQLite
  index can be deleted and rebuilt.

## Status

PoC, v1 feature-complete against the [design](docs/DESIGN.md). All ten acceptance criteria
are covered by tests or were checked by hand (see [docs/STATUS.md](docs/STATUS.md)).

## Run it

```sh
docker compose up -d
docker compose logs | grep -A2 "shown once"   # the first-start admin token
open http://localhost:8080                     # paste the token to sign in
```

Data goes to `./data` (set `ALEXANDRIA_VOLUME=/volume1/alexandria` on a NAS). The volume
must be **local storage** (no NFS/SMB) and writable by uid 1000.

More tokens: `docker compose exec alexandria alexandria token create claude --scope write`.

## Connect an agent

```sh
claude mcp add --transport http alexandria http://<host>:8080/mcp \
  --header "Authorization: Bearer <token>"
```

Tools: `search`, `get_resource`, `get_content`, `list_recent`, `list_tags` (read);
`ingest_url`, `create_note`, `update_resource`, `attach_url`, `refetch` (write). Fetched
text is returned under `untrusted_content`.

REST API at `/api/v1` (docs at `/api/docs`). The UI's Settings page has a bookmarklet.

## Restore a repository archive

```sh
tar -xzf repository.git.tar.gz && git clone repository.git my-checkout
```

## Back up

`resources/` and `tokens.yaml` are plain files: rsync or NAS snapshots are safe.
`alexandria backup <dir>` also copies the database with `VACUUM INTO`; or rebuild it with
`alexandria reindex` (only the job queue is lost).

## Configuration

Environment variables `ALEXANDRIA_*` (nested with `__`, e.g. `ALEXANDRIA_GITHUB__TOKEN`)
over `<data>/config.yaml`. See [docs/DESIGN.md §13](docs/DESIGN.md#13-configuration).

## Docs

- [docs/DESIGN.md](docs/DESIGN.md) — the v1 design.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the code is organized.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — running, testing, the gate.
- [docs/adrs/](docs/adrs/README.md) — decisions and their costs.
- [docs/STATUS.md](docs/STATUS.md) — acceptance criteria, known limits, next steps.
