# Development

Python 3.14 and Poetry. Docker for the image. `git` on the PATH for repository fetches.

```sh
poetry install
export ALEXANDRIA_DATA_DIR=$PWD/dev-data          # never /data on a laptop
poetry run alexandria token create me --scope admin
poetry run alexandria serve --port 8080           # UI on http://localhost:8080
```

## The gate

Run all of it before committing:

```sh
poetry run ruff format --check src tests
poetry run ruff check src tests
poetry run mypy
poetry run pytest
```

The suite never touches the network (acceptance criterion 10): `tests/conftest.py` swaps
the DNS resolver and makes `socket.getaddrinfo` / `create_connection` raise. HTTP is mocked
with respx; git tests clone local fixture repositories over `file://`, which only the test
fetcher allows (`GitRepoFetcher(allowed_protocols=("https", "file"))`).

## Tests: where things go

- `tests/test_core.py` — pure functions: URLs, slugs, SSRF rules, paths, chunking.
- `tests/test_library.py` — the library against a temp data dir: fetch, retry, refetch,
  duplicates, inbox, reindex equivalence, attachments, git full/shallow and restore.
- `tests/test_interfaces.py` — REST, MCP (raw JSON-RPC) and UI through `TestClient` with
  the real lifespan and worker.
- `tests/test_cli.py` — the command line through Typer's `CliRunner`, on the test data
  dir; `drain()` runs the queued jobs as the server would.
- `tests/test_media.py`, `tests/test_proxy.py` — videos and the guarded proxy. yt-dlp is
  replaced by `tests/fake_ytdlp.py` (an autouse fixture); `TestFormatChoice` runs the real
  yt-dlp format selector offline.
- Each feature gets its CLI command and a test in `test_cli.py` (AGENTS.md).

## Docker

```sh
docker compose up --build    # data in ./data, or ALEXANDRIA_VOLUME=/volume1/alexandria
docker build --build-context git=.git -t alexandria:latest .   # the same, by hand
docker compose logs | grep -A2 "shown once"   # first-start admin token
```

The `git` build context names the build (`git describe`, shown in the footer); without
it, `docker build .` still works and records only the date. `--build-arg MEDIA=slim`
leaves out ffmpeg and Deno (transcripts only).

A bind-mounted directory must be writable by uid 1000 (`chown 1000:1000 <dir>`); a named
volume works as is. Multi-arch: `docker buildx build --platform linux/amd64,linux/arm64 .`

## Traps

- `ALEXANDRIA_DATA_DIR` is read twice: by settings and to locate `config.yaml`.
- Templates use no inline `style=` or `<script>`: the CSP forbids them (ADR-0010). Put
  styles in `ui/static/app.css` (colours only as `var(--…)` tokens) and behaviour in
  `ui/static/app.js`.
- A change to the frontmatter shape must keep old files readable, or `reindex` reports them.
