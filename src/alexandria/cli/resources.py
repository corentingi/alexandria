"""Commands on resources: save, find, read, change, re-fetch, delete."""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import typer

from alexandria.cli._common import (
    CLI_USER,
    Json,
    Keep,
    Limit,
    MaxHeight,
    Project,
    Tags,
    Why,
    Yes,
    app,
    attachment_app,
    fail,
    handled,
    open_library,
    print_json,
    print_row,
    run,
    setup_logging,
    snapshot_app,
    warn,
)
from alexandria.library import InvalidInput, Library, Update
from alexandria.models import FetchStatus, Kind, Resource, Scope, Status, iso
from alexandria.storage.index import SearchFilters
from alexandria.views import highlight, resource_detail, resource_summary, search_result

# Filters shared by list, search and refetch.
KindOpt = Annotated[Kind | None, typer.Option("--kind", help="Only this kind")]
StatusOpt = Annotated[
    Status | None,
    typer.Option("--status", help="Only this status (archived is otherwise left out)"),
]
FetchOpt = Annotated[
    FetchStatus | None, typer.Option("--fetch-status", help="Only this fetch status")
]


def _filters(
    kind: Kind | None = None,
    status: Status | None = None,
    tags: list[str] | None = None,
    project: str | None = None,
    fetch_status: FetchStatus | None = None,
) -> SearchFilters:
    return SearchFilters(
        kind=kind.value if kind else None,
        status=status.value if status else None,
        tags=tags or [],
        project=project,
        fetch_status=fetch_status.value if fetch_status else None,
    )


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            yield block


async def run_now(library: Library, job_ids: list[str | None], timeout: float) -> None:
    """Run these jobs in this process. Other queued jobs are left to the server."""
    from alexandria.jobs import Worker

    try:
        await Worker(library).drain(timeout=timeout, only={j for j in job_ids if j})
    except TimeoutError:
        warn("Still running after the time limit; queued again for the server.")


# ------------------------------------------------------------------------ save


@app.command()
@handled
def add(
    url: str,
    why: Why = None,
    tag: Tags = None,
    project: Project = None,
    keep: Keep = None,
    max_height: MaxHeight = None,
    video: Annotated[
        bool, typer.Option("--video", help="Save as a video or audio page (any yt-dlp site)")
    ] = False,
    now: Annotated[bool, typer.Option("--now", help="Fetch in this process")] = False,
) -> None:
    """Save a URL (article, git repository or video)."""
    setup_logging("WARNING")
    library = open_library()

    async def go() -> None:
        result = await library.ingest(
            url, why, tag or [], project=project, created_by=CLI_USER,
            kind=Kind.MEDIA if video else None, media=library.media_choice(keep, max_height),
        )  # fmt: skip
        r = result.resource
        if result.duplicate:
            typer.echo(f"Already saved: {r.slug} ({r.id})")
            return
        typer.echo(f"Saved {r.slug} ({r.id}), status {r.status.value}; fetch queued.")
        if now:
            await run_now(library, [result.job_id], library.settings.git.clone_timeout_s * 2)
            r = library.get(r.id)
            typer.echo(f"fetch: {r.fetch_status.value} {r.fetch_error or ''}".rstrip())

    run(go())


@app.command()
def upload(
    files: Annotated[list[Path], typer.Argument(help="PDF, DOCX, EPUB, HTML, Markdown, text…")],
    why: Why = None,
    tag: Tags = None,
    project: Project = None,
    now: Annotated[bool, typer.Option("--now", help="Extract in this process")] = False,
) -> None:
    """Save documents. Text is extracted in the background (or now, with --now)."""
    setup_logging("WARNING")
    library = open_library()

    async def go() -> None:
        jobs: list[str | None] = []
        for path in files:
            if not path.is_file():
                warn(f"skipped {path}: not a file")
                continue
            try:
                result = await library.ingest_file(
                    path.name, _file_chunks(path), why=why, tags=tag or [], project=project,
                    created_by=CLI_USER,
                )  # fmt: skip
            except InvalidInput as exc:  # one bad file must not stop the others
                typer.secho(f"{path}: {exc}", fg="red", err=True)
                continue
            word = "Already saved" if result.duplicate else "Saved"
            typer.echo(f"{word}: {result.resource.slug} ({result.resource.id})")
            jobs.append(result.job_id)
        if now:
            await run_now(library, jobs, 3600)

    run(go())


@app.command("import")
@handled
def import_(
    source: Annotated[Path, typer.Argument(help="Text with URLs, or a bookmarks .html export")],
    why: Why = None,
    tag: Tags = None,
    project: Project = None,
    keep: Annotated[
        str | None, typer.Option(help="For the videos: transcript, audio or video")
    ] = None,
    max_height: MaxHeight = None,
) -> None:
    """Save every link in a file."""
    setup_logging("WARNING")
    library = open_library()
    text = source.read_text(encoding="utf-8", errors="replace")
    media = library.media_choice(keep, max_height)
    report = run(library.import_urls(text, why, tag or [], project, CLI_USER, media))
    for error in report.errors:
        warn(error)
    typer.echo(
        f"{len(report.created)} saved, {len(report.duplicates)} already there, "
        f"{len(report.errors)} rejected. Fetches are queued."
    )


@app.command()
@handled
def note(
    title: str,
    body_file: Annotated[Path | None, typer.Option(help="Markdown file; '-' for stdin")] = None,
    why: Why = None,
    tag: Tags = None,
    project: Project = None,
) -> None:
    """Save a note."""
    body = _read_text(body_file)
    library = open_library()
    note_ = run(library.create_note(title, body or "", why, tag or [], None, project, CLI_USER))
    typer.echo(f"Saved note {note_.slug} ({note_.id})")


def _read_text(path: Path | None) -> str | None:
    if path is None:
        return None
    if str(path) == "-":
        return typer.get_text_stream("stdin").read()
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------------ find


@app.command("list")
def list_(
    kind: KindOpt = None,
    status: StatusOpt = None,
    tag: Tags = None,
    project: Project = None,
    fetch_status: FetchOpt = None,
    limit: Limit = 20,
    as_json: Json = False,
) -> None:
    """The most recently saved resources, newest first."""
    library = open_library()
    hits = library.search("", _filters(kind, status, tag, project, fetch_status), limit)
    rows = [search_result(h, r) for h, r in hits]
    if as_json:
        print_json(rows)
        return
    if not rows:
        typer.echo("Nothing.")
    for row in rows:
        print_row(row)


@app.command()
def search(
    query: Annotated[str, typer.Argument()] = "",
    kind: KindOpt = None,
    status: StatusOpt = None,
    tag: Tags = None,
    project: Project = None,
    limit: Limit = 10,
    as_json: Json = False,
) -> None:
    """Search the library (words in titles, reasons, notes, tags and content)."""
    library = open_library()
    hits = library.search(query, _filters(kind, status, tag, project), limit)
    if as_json:
        print_json([search_result(h, r) for h, r in hits])
        return
    if not hits:
        typer.echo("No results.")
    for hit, row in hits:
        print_row(search_result(hit, row) | {"why": None})
        if row.get("why"):
            typer.echo(f"  why: {row['why']}")
        if hit.snippet:
            typer.echo(f"  … {highlight(hit.snippet, '[', ']')}")
        typer.echo("")


@app.command()
@handled
def lookup(url: str, as_json: Json = False) -> None:
    """Whether a URL is already saved (compared canonically), without saving it."""
    found = open_library().lookup_url(url)
    if as_json:
        print_json({"saved": True, **resource_summary(found)} if found else {"saved": False})
    elif found is None:
        typer.echo("Not saved.")
    else:
        print_row(resource_summary(found))


@app.command()
def tags() -> None:
    """Every tag, with how many resources have it."""
    for tag, count in open_library().tags():
        typer.echo(f"{count:>6}  {tag}")


@app.command()
def projects() -> None:
    """Every project, with how many resources it has."""
    for project, count in open_library().projects():
        typer.echo(f"{count:>6}  {project}")


# ------------------------------------------------------------------------ read


@app.command()
@handled
def show(ref: str, as_json: Json = False) -> None:
    """A resource's details: status, reason, tags, fetch, files (REF: id or slug)."""
    library = open_library()
    resource = library.get(ref)
    if as_json:
        print_json(resource_detail(resource, library.chunks(resource.id)))
        return
    _print_resource(resource)


def _print_resource(r: Resource) -> None:
    typer.secho(r.title, bold=True)
    facts = [
        ("id", r.id), ("slug", r.slug), ("kind", r.kind.value), ("status", r.status.value),
        ("url", r.canonical_url or r.source_url), ("why", r.why),
        ("tags", ", ".join(r.tags)), ("project", r.project),
        ("captured", iso(r.captured_at)), ("by", r.created_by),
        ("fetch", f"{r.fetch_status.value} {r.fetch_error or ''}".rstrip()),
    ]  # fmt: skip
    if r.media:
        height = f" up to {r.media.max_height}p" if r.media.keep.value == "video" else ""
        facts.append(("keeps", f"{r.media.keep.value}{height}"))
    for key in ("media_height", "media_skipped", "channel", "duration_s"):
        if r.meta.get(key):
            facts.append((key, str(r.meta[key])))
    for key, value in facts:
        if value:
            typer.echo(f"  {key:<12} {value}")
    if r.notes and r.kind != Kind.NOTE:
        typer.echo(f"  notes        {r.notes}")
    snapshot = next((s for s in r.snapshots if s.id == r.current_snapshot), None)
    if snapshot:
        typer.echo(f"  snapshot     {snapshot.id} ({len(r.snapshots)} in all)")
        for a in snapshot.artifacts:
            typer.echo(f"    {a.path:<28} {a.size / 1e6:>9.1f} MB  {a.role}")
    for att in r.attachments:
        typer.echo(f"  attachment   {att.path} ({att.size / 1e6:.1f} MB)")


@app.command()
@handled
def content(
    ref: str,
    chunk: Annotated[int | None, typer.Option(help="Only this chunk (see show --json)")] = None,
) -> None:
    """Print a resource's text (the article, transcript, document or note)."""
    library = open_library()
    if chunk is not None:
        typer.echo(library.content(ref, chunk, max_chars=10**9)["text"])
        return
    text, _ = library.text(library.get(ref))
    if not text:
        fail("this resource has no text yet")
    typer.echo(text)


# ---------------------------------------------------------------------- change


@app.command()
@handled
def edit(
    ref: str,
    title: Annotated[str | None, typer.Option(help="A title of your own")] = None,
    why: Why = None,
    notes: Annotated[str | None, typer.Option(help="Replaces the notes")] = None,
    notes_file: Annotated[
        Path | None, typer.Option(help="Notes from a file; '-' for stdin")
    ] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Add a tag (repeatable)")] = None,
    untag: Annotated[
        list[str] | None, typer.Option("--untag", help="Remove a tag (repeatable)")
    ] = None,
    project: Annotated[
        str | None, typer.Option(help="Move to this project; '' for the global library")
    ] = None,
    status: Annotated[Status | None, typer.Option(help="inbox, kept or archived")] = None,
    keep: Keep = None,
    max_height: MaxHeight = None,
) -> None:
    """Change a resource's title, reason, notes, tags, project, status or video options.

    Options left out are left as they are. Video options apply from the next fetch
    (alexandria refetch).
    """
    library = open_library()
    notes = _read_text(notes_file) if notes_file else notes
    changes = Update(
        title=title, why=why, notes=notes, status=status,
        project=project, scope=Scope.GLOBAL if project == "" else None,
        media=library.media_choice(keep, max_height) if keep or max_height else None,
        add_tags=tag or None, remove_tags=untag or None,
    )  # fmt: skip
    if max_height and not keep:
        fail("--max-height needs --keep video")
    if changes == Update():
        fail("nothing to change: give at least one option (see --help)")
    resource = run(library.update(ref, changes))
    typer.echo(f"Updated {resource.slug}.")
    if changes.media:
        typer.echo(
            "The video options apply from the next fetch: alexandria refetch " + resource.slug
        )


@app.command()
@handled
def refetch(
    refs: Annotated[list[str] | None, typer.Argument(help="Ids or slugs")] = None,
    kind: KindOpt = None,
    status: StatusOpt = None,
    tag: Tags = None,
    project: Project = None,
    fetch_status: FetchOpt = None,
    every: Annotated[bool, typer.Option("--all", help="Every resource (archived aside)")] = False,
    yes: Yes = False,
) -> None:
    """Fetch resources again: the ones named, or every one matching filters.

    Examples: refetch my-article; refetch --kind media; refetch --fetch-status failed.
    Archived resources are left out unless --status archived. A video's file is
    downloaded again only when its options changed or a taller format is offered.
    """
    library = open_library()
    filtered = any(x is not None for x in (kind, status, project, fetch_status)) or bool(tag)
    if refs and (filtered or every):
        fail("name resources, or use filters / --all, not both")
    if not refs and not (filtered or every):
        fail("name resources, or select them with filters (see --help) or --all")
    if refs:
        targets = list(refs)
    else:
        targets = library.select(_filters(kind, status, tag, project, fetch_status))
        notes = library.select(_filters(Kind.NOTE, status, tag, project, fetch_status))
        targets = [t for t in targets if t not in set(notes)]  # nothing to fetch
        if not targets:
            typer.echo("No resource matches.")
            return
        if not yes:
            typer.confirm(f"Fetch {len(targets)} resource(s) again?", abort=True)
    report = run(library.refetch_many(targets))
    for ref, reason in report.skipped:
        warn(f"{ref}: {reason}")
    typer.echo(
        f"{len(report.queued)} fetch(es) queued, {len(report.skipped)} skipped. "
        "The server runs them; follow with alexandria jobs list."
    )
    if refs and report.skipped:
        raise typer.Exit(1)


@app.command()
@handled
def delete(ref: str, yes: Yes = False) -> None:
    """Delete a resource and all its files."""
    library = open_library()
    resource = library.get(ref)
    if not yes:
        typer.confirm(f"Delete {resource.title!r} and all its files?", abort=True)
    run(library.delete(resource.id))
    typer.echo("Deleted.")


# ------------------------------------------------------------ files of a resource


@attachment_app.command("add")
@handled
def attachment_add(
    ref: str,
    source: Annotated[str, typer.Argument(help="A file path, or a URL to download")],
) -> None:
    """Attach a file, or download one from a URL in the background."""
    library = open_library()
    path = Path(source)
    if path.is_file():
        mime = mimetypes.guess_type(path.name)[0]
        att = run(library.attach_upload(ref, path.name, _file_chunks(path), mime, CLI_USER))
        typer.echo(f"Attached {att.path} ({att.size / 1e6:.1f} MB).")
    elif source.startswith(("http://", "https://")):
        job_id = run(library.attach_url(ref, source, CLI_USER))
        typer.echo(f"Download queued (job {job_id}).")
    else:
        fail(f"{source}: no such file, and not an http(s) URL")


@attachment_app.command("delete")
@handled
def attachment_delete(ref: str, path: str, yes: Yes = False) -> None:
    """Delete an attachment (PATH as in show; with or without 'attachments/')."""
    path = path.removeprefix("attachments/")
    if not yes:
        typer.confirm(f"Delete attachment {path}?", abort=True)
    run(open_library().delete_attachment(ref, path))
    typer.echo("Deleted.")


@attachment_app.command("extract")
@handled
def attachment_extract(ref: str, path: str) -> None:
    """Extract an attachment's text again, in the background."""
    job_id = open_library().extract_attachment(ref, path.removeprefix("attachments/"))
    typer.echo(f"Extraction queued (job {job_id}).")


@snapshot_app.command("list")
@handled
def snapshot_list(ref: str) -> None:
    """A resource's snapshots, oldest first, with their size."""
    r = open_library().get(ref)
    for snap in r.snapshots:
        size = sum(a.size for a in snap.artifacts) / 1e6
        mark = "  (current)" if snap.id == r.current_snapshot else ""
        typer.echo(f"{snap.id}  {size:>9.1f} MB  {len(snap.artifacts)} file(s){mark}")


@snapshot_app.command("delete")
@handled
def snapshot_delete(ref: str, snapshot_id: str, yes: Yes = False) -> None:
    """Delete one snapshot and its files (not the only one).

    If it is the current one, the newest remaining becomes current.
    """
    if not yes:
        typer.confirm(f"Delete snapshot {snapshot_id}?", abort=True)
    run(open_library().delete_snapshot(ref, snapshot_id))
    typer.echo("Deleted.")
