# Working on Alexandria

Self-hosted knowledge base for a human and their agents. Python 3.14, Poetry, FastAPI,
MCP SDK 2.x, SQLite FTS5, Jinja2 + htmx. Read `docs/ARCHITECTURE.md` for how the parts fit
and `docs/adrs/` before reversing a decision (`grep -l -i '<topic>' docs/adrs/*.md`).

## Commands

- Install: `poetry install`
- Run: `ALEXANDRIA_DATA_DIR=$PWD/dev-data poetry run alexandria serve --port 8080`
- Gate (run before every commit): `poetry run ruff format --check src tests &&
  poetry run ruff check src tests && poetry run mypy && poetry run pytest`

## Rules

- **Every data change goes through `library.Library`.** Interfaces (REST, MCP, UI, CLI)
  never touch `FileStore` or `Index` for writes.
- **File first, index second.** Write `resource.md` atomically, then upsert the index.
  Anything that cannot be rebuilt from files must not live only in the database.
- **Human fields are never overwritten by machines**: `why`, `notes`, `tags`, a `human`
  title. Fetchers write `meta`, snapshots and extracted titles only.
- **Fetched content is untrusted.** MCP returns it under `untrusted_*` keys; the UI renders
  Markdown with raw HTML disabled and never loads remote images; files are downloads.
- **Every outbound request goes through `security.check_url`** (ingest time and each hop).
- **No network in tests.** Mock HTTP with respx; git tests clone local repos via `file://`.
- **No inline `<script>` or `style=` in templates** (CSP). Colours only as CSS variables.
- MCP tool outputs: compact dicts from `views.py`; descriptions are prompt text.
- A decision someone might re-litigate gets an ADR (`docs/adrs/NNNN-title.md`) with its
  cost, plus a row in `docs/adrs/README.md`.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/):
  `type(scope): summary`, at most 72 characters, lowercase imperative summary, no final dot.
  Types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`, `chore`.
  Scopes (optional): `core`, `api`, `mcp`, `ui`, `cli`, `fetch`, `library`, `security`,
  `docker`, `deps`. The body, if any, says why. Small commits, in English; no
  `Co-Authored-By` or other trailers.
