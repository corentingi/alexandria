"""Command line (design §10.4). Commands work on the data dir directly.

``add`` and ``note`` write files and the index like the API does; the running server's
worker picks queued fetches up from the shared SQLite queue. ``--now`` fetches in-process.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Annotated

import typer

from alexandria import __version__
from alexandria.auth import ScopeLevel, TokenStore
from alexandria.config import Settings, load_settings
from alexandria.library import InvalidInput, Library, NotFound
from alexandria.storage.index import SearchFilters
from alexandria.views import highlight

app = typer.Typer(help="Alexandria: a personal knowledge base for humans and agents.")
token_app = typer.Typer(help="Manage API tokens.")
app.add_typer(token_app, name="token")
library_app = typer.Typer(help="Export or import the whole library as tar parts.")
app.add_typer(library_app, name="library")


def _settings() -> Settings:
    return load_settings()


def _library() -> Library:
    return Library(_settings())


def _fail(message: str) -> None:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(1)


def _logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Overrides http.host")] = None,
    port: Annotated[int | None, typer.Option(help="Overrides http.port")] = None,
    log_level: str = "INFO",
) -> None:
    """Run the web UI, REST API, MCP endpoint and job worker."""
    import uvicorn

    from alexandria.app import create_app

    _logging(log_level)
    settings = _settings()
    asgi = create_app(settings)
    uvicorn.run(
        asgi,
        host=host or settings.http.host,
        port=port or settings.http.port,
        log_level=log_level.lower(),
        proxy_headers=False,
    )


@app.command()
def add(
    url: str,
    why: Annotated[str | None, typer.Option(help="Why is this worth keeping?")] = None,
    tag: Annotated[list[str] | None, typer.Option(help="Repeatable")] = None,
    project: str | None = None,
    now: Annotated[bool, typer.Option("--now", help="Fetch in this process")] = False,
) -> None:
    """Save a URL (article or git repository)."""
    _logging("WARNING")
    library = _library()

    async def run() -> None:
        try:
            result = await library.ingest(url, why, tag or [], project=project, created_by="cli")
        except InvalidInput as exc:
            _fail(str(exc))
            return
        r = result.resource
        if result.duplicate:
            typer.echo(f"Already saved: {r.slug} ({r.id})")
            return
        typer.echo(f"Saved {r.slug} ({r.id}), status {r.status.value}; fetch queued.")
        if now:
            from alexandria.jobs import Worker

            await Worker(library).drain(timeout=library.settings.git.clone_timeout_s * 2)
            r = library.get(r.id)
            typer.echo(f"fetch: {r.fetch_status.value} {r.fetch_error or ''}".rstrip())

    asyncio.run(run())


@app.command()
def upload(
    files: Annotated[list[Path], typer.Argument(help="PDF, DOCX, EPUB, HTML, Markdown, text…")],
    why: str | None = None,
    tag: Annotated[list[str] | None, typer.Option()] = None,
    project: str | None = None,
    now: Annotated[bool, typer.Option("--now", help="Extract in this process")] = False,
) -> None:
    """Save documents. Text is extracted in the background (or now, with --now)."""
    _logging("WARNING")
    library = _library()

    async def chunks(path: Path):  # type: ignore[no-untyped-def]
        with path.open("rb") as fh:
            while block := fh.read(1 << 20):
                yield block

    async def run() -> None:
        for path in files:
            if not path.is_file():
                typer.secho(f"skipped {path}: not a file", fg="yellow", err=True)
                continue
            try:
                result = await library.ingest_file(
                    path.name, chunks(path), why=why, tags=tag or [], project=project,
                    created_by="cli",
                )  # fmt: skip
            except InvalidInput as exc:
                typer.secho(f"{path}: {exc}", fg="red", err=True)
                continue
            word = "Already saved" if result.duplicate else "Saved"
            typer.echo(f"{word}: {result.resource.slug} ({result.resource.id})")
        if now:
            from alexandria.jobs import Worker

            await Worker(library).drain(timeout=3600)

    asyncio.run(run())


@app.command("import")
def import_(
    source: Annotated[Path, typer.Argument(help="Text with URLs, or a bookmarks .html export")],
    why: str | None = None,
    tag: Annotated[list[str] | None, typer.Option()] = None,
    project: str | None = None,
) -> None:
    """Save every link in a file."""
    _logging("WARNING")
    library = _library()
    text = source.read_text(encoding="utf-8", errors="replace")
    report = asyncio.run(library.import_urls(text, why, tag or [], project, "cli"))
    for error in report.errors:
        typer.secho(error, fg="yellow", err=True)
    typer.echo(
        f"{len(report.created)} saved, {len(report.duplicates)} already there, "
        f"{len(report.errors)} rejected. Fetches are queued."
    )


@app.command()
def note(
    title: str,
    body_file: Annotated[Path | None, typer.Option(help="Markdown file; '-' for stdin")] = None,
    why: str | None = None,
    tag: Annotated[list[str] | None, typer.Option()] = None,
    project: str | None = None,
) -> None:
    """Save a note."""
    if body_file is None:
        body = ""
    elif str(body_file) == "-":
        body = typer.get_text_stream("stdin").read()
    else:
        body = body_file.read_text(encoding="utf-8")
    library = _library()
    note_ = asyncio.run(
        library.create_note(title, body, why, tag or [], None, project, created_by="cli")
    )
    typer.echo(f"Saved note {note_.slug} ({note_.id})")


@app.command()
def search(
    query: Annotated[str, typer.Argument()] = "",
    kind: str | None = None,
    tag: Annotated[list[str] | None, typer.Option()] = None,
    limit: int = 10,
) -> None:
    """Search the library."""
    library = _library()
    hits = library.search(query, SearchFilters(kind=kind, tags=tag or []), limit)
    if not hits:
        typer.echo("No results.")
    for hit, row in hits:
        typer.secho(f"{row['title']}", bold=True, nl=False)
        typer.echo(f"  [{row['kind']}] {row['slug']}")
        if row.get("canonical_url"):
            typer.echo(f"  {row['canonical_url']}")
        if row.get("why"):
            typer.echo(f"  why: {row['why']}")
        if hit.snippet:
            typer.echo(f"  … {highlight(hit.snippet, '[', ']')}")
        typer.echo("")


@app.command()
def reindex(ref: Annotated[str | None, typer.Argument()] = None) -> None:
    """Rebuild the index from the resource files (all, or one resource)."""
    library = _library()
    report = asyncio.run(library.reindex(ref))
    for error in report.errors:
        typer.secho(f"invalid: {error}", fg="yellow", err=True)
    typer.echo(f"Indexed {report.indexed} resource(s), {len(report.errors)} error(s).")


@app.command()
def backup(dest_dir: Path) -> None:
    """Copy the database (VACUUM INTO) and the resource files to DEST_DIR."""
    settings = _settings()
    library = Library(settings)
    dest_dir.mkdir(parents=True, exist_ok=True)
    db_dest = dest_dir / "alexandria.db"
    if db_dest.exists():
        _fail(f"{db_dest} already exists")
    library.index.backup(db_dest)
    shutil.copytree(settings.resources_dir, dest_dir / "resources", dirs_exist_ok=True)
    if settings.tokens_path.exists():
        shutil.copy2(settings.tokens_path, dest_dir / "tokens.yaml")
    typer.echo(f"Backed up to {dest_dir}")


@app.command()
def delete(ref: str, yes: Annotated[bool, typer.Option("--yes")] = False) -> None:
    """Delete a resource and all its files."""
    library = _library()
    try:
        resource = library.get(ref)
    except NotFound as exc:
        _fail(str(exc))
        return
    if not yes:
        typer.confirm(f"Delete {resource.title!r} and all its files?", abort=True)
    asyncio.run(library.delete(resource.id))
    typer.echo("Deleted.")


@token_app.command("create")
def token_create(
    name: str,
    scope: Annotated[str, typer.Option(help="read | write | admin")] = "read",
) -> None:
    """Create a token. The secret is printed once."""
    store = TokenStore(_settings().tokens_path)
    try:
        secret = store.create(name, ScopeLevel.parse(scope))
    except ValueError as exc:
        _fail(str(exc))
        return
    typer.echo(secret)


@token_app.command("list")
def token_list() -> None:
    """List tokens (names and scopes only)."""
    for t in TokenStore(_settings().tokens_path).list():
        typer.echo(f"{t['name']:<24} {t['scope']:<6} {t.get('created_at', '')}")


@token_app.command("scope")
def token_scope(
    name: str, scope: Annotated[str, typer.Argument(help="read | write | admin")]
) -> None:
    """Change a token's scope. The command line may remove the last admin (recovery)."""
    try:
        TokenStore(_settings().tokens_path).set_scope(
            name, ScopeLevel.parse(scope), keep_an_admin=False
        )
    except ValueError as exc:
        _fail(str(exc))
    typer.echo(f"{name} is now {scope.lower()}.")


@token_app.command("revoke")
def token_revoke(name: str) -> None:
    """Revoke a token by name."""
    if not TokenStore(_settings().tokens_path).revoke(name):
        _fail(f"no token named {name!r}")
    typer.echo(f"Revoked {name}.")


if __name__ == "__main__":
    app()


@library_app.command("export")
def library_export(
    out: Annotated[
        Path | None, typer.Option(help="Directory for the parts (default: <data>/exports/<id>)")
    ] = None,
) -> None:
    """Export every resource into tar parts of about export.part_mb (2000 MB) each."""
    from alexandria.models import utcnow
    from alexandria.storage.files import SNAPSHOT_FORMAT

    library = _library()
    export_id = utcnow().strftime(SNAPSHOT_FORMAT)
    directory = out / export_id if out else None
    info = asyncio.run(library.export_library(export_id, directory))
    where = directory or library.settings.exports_dir / export_id
    for part in info.parts:
        typer.echo(f"{where / part.name}  {part.size / 1e6:.1f} MB  {part.resources} resources")
    typer.echo(f"Exported {info.resources} resource(s) in {len(info.parts)} part(s).")


@library_app.command("import")
def library_import(
    parts: Annotated[list[Path], typer.Argument(help="Export parts (.tar), in any order")],
) -> None:
    """Import export parts. Resources already present (same id or link) are skipped."""
    library = _library()
    failed = False
    for path in parts:
        if not path.is_file():
            _fail(f"{path}: no such file")
        try:
            report = asyncio.run(library.import_library(path))
        except InvalidInput as exc:
            typer.secho(str(exc), fg="red", err=True)
            failed = True
            continue
        for error in report.errors:
            typer.secho(f"{path.name}: {error}", fg="yellow", err=True)
        typer.echo(
            f"{path.name}: {len(report.imported)} imported, {len(report.existing)} already here, "
            f"{len(report.duplicates)} duplicate links, {len(report.errors)} problem(s)"
        )
    if failed:
        raise typer.Exit(1)
