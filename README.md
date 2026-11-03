# Alexandria

A self-hosted personal knowledge base shared by a human and their AI agents. Give it a URL
and a reason; it fetches, archives and indexes the content, and remembers **why** you kept
it. Agents read and write through MCP, you use a small web UI. One container, one volume.

- **Articles** — raw HTML, extracted Markdown, metadata, full-text search.
- **Git repositories** — a bare clone (full history, or the last 1,000 commits for huge
  repos) packed as `repository.git.tar.gz`, plus GitHub metadata and README.
- **Documents** — upload PDF, DOCX, EPUB, HTML, Markdown or text; the original is kept and
  its text extracted and indexed (links to such files get the same treatment). Any other
  file is kept too, ready to be analysed later.
- **Notes** — Markdown, searchable at once.
- **Bulk import** — paste links or upload a browser bookmarks export.
- **Files are the truth** — every resource is a folder with a `resource.md`; the SQLite
  index can be deleted and rebuilt.

## Status

PoC, v1 feature-complete against the [design](docs/DESIGN.md), plus documents and bulk
import ([ADR-0012](docs/adrs/0012-documents-and-text-extraction.md),
[ADR-0013](docs/adrs/0013-bulk-link-import.md)). All ten acceptance criteria
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

REST API at `/api/v1` (docs at `/api/docs`), including `POST /documents` (multipart) and
`POST /import`. The UI's Settings page has a bookmarklet. CLI: `alexandria add <url>`,
`alexandria upload <files…>`, `alexandria import <bookmarks.html>`, `alexandria note`.

## Restore a repository archive

```sh
tar -xzf repository.git.tar.gz && git clone repository.git my-checkout
```

## Back up

`resources/` and `tokens.yaml` are plain files: rsync or NAS snapshots are safe.
`alexandria backup <dir>` also copies the database with `VACUUM INTO`; or rebuild it with
`alexandria reindex` (only the job queue is lost).

## Move a library

Settings → *Export & import the library* writes the whole library as plain `.tar` parts
of about 2 GB each (`export.part_mb`). Each part stands alone: import them in any order,
here or on another installation. Anything already present is skipped, never overwritten.
From a shell: `alexandria library export [--out DIR]` and
`alexandria library import <parts…>`. See
[ADR-0015](docs/adrs/0015-library-export-and-import.md).

## Configuration

Environment variables `ALEXANDRIA_*` (nested with `__`, e.g. `ALEXANDRIA_GITHUB__TOKEN`)
over `<data>/config.yaml`. See [docs/DESIGN.md §13](docs/DESIGN.md#13-configuration).

## Docs

- [docs/DESIGN.md](docs/DESIGN.md) — the v1 design.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the code is organized.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — running, testing, the gate.
- [docs/adrs/](docs/adrs/README.md) — decisions and their costs.
- [docs/STATUS.md](docs/STATUS.md) — acceptance criteria, known limits, next steps.

## Licence

[GNU Affero General Public License v3.0](LICENSE) or later. You may use, change and share
Alexandria; if you offer a modified version to others, including over a network, you must
make its source available under the same licence.
