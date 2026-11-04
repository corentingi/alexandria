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

From the published image (on a NAS: see [docs/NAS.md](docs/NAS.md)):

```sh
docker run -d --name alexandria -p 8080:8080 -v "$PWD/data:/data" \
  ghcr.io/corentingi/alexandria:latest
docker logs alexandria | grep -A2 "shown once"   # the first-start admin token
open http://localhost:8080                       # paste the token to sign in
```

Or build from source with `docker compose up -d --build` (data in `./data`, or set
`ALEXANDRIA_VOLUME`). The volume must be **local storage** (no NFS/SMB) and writable by
uid 1000.

More tokens: Settings → Tokens (create, change scope, revoke), or
`docker exec alexandria alexandria token create claude --scope write`.

## Connect an agent

```sh
claude mcp add --transport http alexandria http://<host>:8080/mcp \
  --header "Authorization: Bearer <token>"
```

Tools: `search`, `get_resource`, `get_content`, `list_recent`, `list_tags`,
`list_projects`, `lookup_url`, `get_job`, `list_jobs` (read); `ingest_url`, `create_note`,
`update_resource`, `attach_url`, `refetch` (write). Fetched text is returned under
`untrusted_content`.

REST API at `/api/v1` (docs at `/api/docs`), including `POST /documents` (multipart) and
`POST /import` (queued as a job: follow it with `GET /jobs/{id}`). The UI's Settings page has a bookmarklet. CLI: `alexandria add <url>`,
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
- [docs/NAS.md](docs/NAS.md) — installing on a NAS from the published image.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — running, testing, the gate.
- [docs/adrs/](docs/adrs/README.md) — decisions and their costs.
- [docs/STATUS.md](docs/STATUS.md) — acceptance criteria, known limits, next steps.

## Security

Alexandria is built for a **home or office network**, not the open internet.

- Every request needs a token. Tokens are random 256-bit secrets, stored only as SHA-256
  hashes in `tokens.yaml`; scopes are `read`, `write` and `admin`. The browser keeps the
  token in an HttpOnly, SameSite cookie, with CSRF protection on every form.
- There is no rate limiting and no TLS of its own. For access from outside, use a VPN
  (Tailscale, WireGuard, your NAS's VPN), not port forwarding. Behind an https reverse
  proxy, set `ALEXANDRIA_HTTP__PUBLIC_URL` so the cookie is marked Secure.
- Fetched pages and files are treated as untrusted: private and local addresses are
  refused (SSRF guard on every redirect), archives and documents are size-capped, the UI
  renders Markdown without raw HTML or remote images under a strict CSP, and agents get
  fetched text under `untrusted_*` keys.
- Report a vulnerability privately through GitHub's *Security → Report a vulnerability*
  on this repository rather than in a public issue.

Decisions and their costs are in [docs/adrs/](docs/adrs/README.md), notably
[ADR-0007](docs/adrs/0007-ssrf-guard-at-ingest-and-every-hop.md) and
[ADR-0010](docs/adrs/0010-ui-security-model.md).

## Licence

[GNU Affero General Public License v3.0](LICENSE) or later. You may use, change and share
Alexandria; if you offer a modified version to others, including over a network, you must
make its source available under the same licence.
