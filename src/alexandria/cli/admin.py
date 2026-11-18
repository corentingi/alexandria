"""Commands to run and look after the server: serve, jobs, index, backup, tokens, transfers."""

from __future__ import annotations

import shutil
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from alexandria.auth import ScopeLevel, TokenStore
from alexandria.build import build
from alexandria.cli._common import (
    Json,
    Limit,
    Yes,
    app,
    fail,
    handled,
    jobs_app,
    library_app,
    open_library,
    print_json,
    run,
    settings,
    setup_logging,
    token_app,
    warn,
)
from alexandria.library import InvalidInput, Library, NotFound
from alexandria.storage.index import Job
from alexandria.views import job_view


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(build().version)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Overrides http.host")] = None,
    port: Annotated[int | None, typer.Option(help="Overrides http.port")] = None,
    log_level: str = "INFO",
) -> None:
    """Run the web UI, REST API, MCP endpoint and job worker."""
    import uvicorn

    from alexandria.app import create_app

    setup_logging(log_level)
    config = settings()
    asgi = create_app(config)
    uvicorn.run(
        asgi,
        host=host or config.http.host,
        port=port or config.http.port,
        log_level=log_level.lower(),
        # Only a configured reverse proxy may say the request came over https.
        proxy_headers=bool(config.http.forwarded_allow_ips),
        forwarded_allow_ips=config.http.forwarded_allow_ips or "127.0.0.1",
    )


@app.command()
def stats() -> None:
    """How many resources per status, and the jobs waiting or failed."""
    library = open_library()
    for status, count in sorted(library.index.counts().items()):
        typer.echo(f"{count:>6}  {status}")
    jobs = library.index.job_counts()
    waiting = {k: v for k, v in jobs.items() if v and k in ("queued", "running", "failed")}
    if waiting:
        typer.echo("jobs: " + ", ".join(f"{v} {k}" for k, v in sorted(waiting.items())))


@app.command()
@handled
def reindex(ref: Annotated[str | None, typer.Argument()] = None) -> None:
    """Rebuild the index from the resource files (all, or one resource)."""
    report = run(open_library().reindex(ref))
    for error in report.errors:
        warn(f"invalid: {error}")
    typer.echo(f"Indexed {report.indexed} resource(s), {len(report.errors)} error(s).")


@app.command()
def backup(dest_dir: Path) -> None:
    """Copy the database (VACUUM INTO) and the resource files to DEST_DIR."""
    config = settings()
    library = Library(config)
    dest_dir.mkdir(parents=True, exist_ok=True)
    db_dest = dest_dir / "alexandria.db"
    if db_dest.exists():
        fail(f"{db_dest} already exists")
    library.index.backup(db_dest)
    shutil.copytree(config.resources_dir, dest_dir / "resources", dirs_exist_ok=True)
    if config.tokens_path.exists():
        shutil.copy2(config.tokens_path, dest_dir / "tokens.yaml")
    typer.echo(f"Backed up to {dest_dir}")


# ------------------------------------------------------------------------ jobs


def _print_job(job: Job) -> None:
    what = job.resource_id or job.payload.get("name") or ""
    line = f"{job.id}  {job.status:<8} {job.type:<18} {job.updated_at}  {what}"
    typer.echo(line.rstrip())
    if job.error and job.status == "failed":
        typer.secho(f"    {job.error}", fg="yellow")


@jobs_app.command("list")
def jobs_list(
    status: Annotated[JobStatus | None, typer.Option(help="Only jobs in this state")] = None,
    limit: Limit = 30,
    as_json: Json = False,
) -> None:
    """Recent jobs, newest first."""
    jobs = open_library().index.jobs(status.value if status else None, limit=limit)
    if as_json:
        print_json([job_view(j) for j in jobs])
        return
    if not jobs:
        typer.echo("No jobs.")
    for job in jobs:
        _print_job(job)


@jobs_app.command("show")
@handled
def jobs_show(job_id: str) -> None:
    """A job, with its payload (an import's report, for example)."""
    job = open_library().index.job(job_id)
    if job is None:
        raise NotFound(f"no job {job_id}")
    print_json(job_view(job))


@jobs_app.command("retry")
@handled
def jobs_retry(
    job_id: Annotated[str | None, typer.Argument()] = None,
    failed: Annotated[bool, typer.Option("--failed", help="Every failed job")] = False,
) -> None:
    """Queue a failed job again (or every one, with --failed)."""
    library = open_library()
    if bool(job_id) == failed:
        fail("give a job id, or --failed")
    ids = [job_id] if job_id else [j.id for j in library.index.jobs("failed", limit=10_000)]
    retried = 0
    for one in ids:
        assert one is not None
        try:
            job = run(library.retry_job(one))
        except InvalidInput as exc:
            if job_id:
                raise
            warn(f"{one}: {exc}")
            continue
        if job is None:
            if job_id:
                raise NotFound(f"no failed job {one}")
            continue
        retried += 1
    typer.echo(f"{retried} job(s) queued again.")


# ---------------------------------------------------------------------- tokens


@token_app.command("create")
def token_create(
    name: str,
    scope: Annotated[str, typer.Option(help="read | write | admin")] = "read",
) -> None:
    """Create a token. The secret is printed once."""
    store = TokenStore(settings().tokens_path)
    try:
        secret = store.create(name, ScopeLevel.parse(scope))
    except ValueError as exc:
        fail(str(exc))
        return
    typer.echo(secret)


@token_app.command("list")
def token_list() -> None:
    """List tokens (names and scopes only)."""
    for t in TokenStore(settings().tokens_path).list():
        typer.echo(f"{t['name']:<24} {t['scope']:<6} {t.get('created_at', '')}")


@token_app.command("scope")
def token_scope(
    name: str, scope: Annotated[str, typer.Argument(help="read | write | admin")]
) -> None:
    """Change a token's scope. The command line may remove the last admin (recovery)."""
    try:
        TokenStore(settings().tokens_path).set_scope(
            name, ScopeLevel.parse(scope), keep_an_admin=False
        )
    except ValueError as exc:
        fail(str(exc))
    typer.echo(f"{name} is now {scope.lower()}.")


@token_app.command("revoke")
def token_revoke(name: str) -> None:
    """Revoke a token by name."""
    if not TokenStore(settings().tokens_path).revoke(name):
        fail(f"no token named {name!r}")
    typer.echo(f"Revoked {name}.")


# --------------------------------------------------------- library transfers


@library_app.command("export")
def library_export(
    out: Annotated[
        Path | None, typer.Option(help="Directory for the parts (default: <data>/exports/<id>)")
    ] = None,
) -> None:
    """Export every resource into tar parts of about export.part_mb (2000 MB) each."""
    from alexandria.models import utcnow
    from alexandria.storage.files import SNAPSHOT_FORMAT

    library = open_library()
    export_id = utcnow().strftime(SNAPSHOT_FORMAT)
    directory = out / export_id if out else None
    info = run(library.export_library(export_id, directory))
    where = directory or library.settings.exports_dir / export_id
    for part in info.parts:
        typer.echo(f"{where / part.name}  {part.size / 1e6:.1f} MB  {part.resources} resources")
    typer.echo(f"Exported {info.resources} resource(s) in {len(info.parts)} part(s).")


@library_app.command("exports")
def library_exports() -> None:
    """Exports kept in the data directory."""
    library = open_library()
    exports = library.exports()
    if not exports:
        typer.echo("No exports.")
    for info in exports:
        typer.echo(
            f"{info.id}  {info.status:<8} {len(info.parts)} part(s)  {info.size / 1e6:.1f} MB  "
            f"{info.resources} resources"
        )


@library_app.command("delete-export")
@handled
def library_delete_export(export_id: str, yes: Yes = False) -> None:
    """Delete an export kept in the data directory."""
    library = open_library()
    if not any(e.id == export_id for e in library.exports()):
        fail(f"no export {export_id}")
    if not yes:
        typer.confirm(f"Delete export {export_id} and its parts?", abort=True)
    library.delete_export(export_id)
    typer.echo("Deleted.")


@library_app.command("import")
def library_import(
    parts: Annotated[list[Path], typer.Argument(help="Export parts (.tar), in any order")],
) -> None:
    """Import export parts. Resources already present (same id or link) are skipped."""
    library = open_library()
    failed = False
    for path in parts:
        if not path.is_file():
            fail(f"{path}: no such file")
        try:
            report = run(library.import_library(path))
        except InvalidInput as exc:
            typer.secho(str(exc), fg="red", err=True)
            failed = True
            continue
        for error in report.errors:
            warn(f"{path.name}: {error}")
        typer.echo(
            f"{path.name}: {len(report.imported)} imported, {len(report.existing)} already here, "
            f"{len(report.duplicates)} duplicate links, {len(report.errors)} problem(s)"
        )
    if failed:
        raise typer.Exit(1)
