"""What every command shares: the Typer apps, the library, errors, output and options."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import typer

from alexandria.config import Settings, load_settings
from alexandria.library import InvalidInput, Library, NotFound

app = typer.Typer(
    help="Alexandria: a personal knowledge base for humans and agents.",
    no_args_is_help=True,
)
token_app = typer.Typer(help="Manage API tokens.", no_args_is_help=True)
library_app = typer.Typer(help="Export or import the whole library as tar parts.",
                          no_args_is_help=True)  # fmt: skip
jobs_app = typer.Typer(help="Background jobs: fetches, extractions, imports.",
                       no_args_is_help=True)  # fmt: skip
attachment_app = typer.Typer(help="Files attached to a resource.", no_args_is_help=True)
snapshot_app = typer.Typer(help="A resource's snapshots (one per fetch).", no_args_is_help=True)
app.add_typer(token_app, name="token")
app.add_typer(library_app, name="library")
app.add_typer(jobs_app, name="jobs")
app.add_typer(attachment_app, name="attachment")
app.add_typer(snapshot_app, name="snapshot")

CLI_USER = "cli"  # recorded as created_by / added_by


def settings() -> Settings:
    return load_settings()


def open_library() -> Library:
    return Library(settings())


def fail(message: str) -> None:
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(1)


def warn(message: str) -> None:
    typer.secho(message, fg="yellow", err=True)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def handled[**P, R](command: Callable[P, R]) -> Callable[P, R]:
    """A refused input or an unknown resource ends the command with its message, exit 1."""

    @functools.wraps(command)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return command(*args, **kwargs)
        except (InvalidInput, NotFound) as exc:
            fail(str(exc))
            raise  # unreachable: fail() exits

    return wrapper


def run[R](coro: Coroutine[Any, Any, R]) -> R:
    return asyncio.run(coro)


def print_json(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def print_row(row: dict[str, Any]) -> None:
    """One resource in a list: title, then kind, status and slug; its URL and why."""
    typer.secho(str(row.get("title") or row.get("slug")), bold=True, nl=False)
    project = f" {row['project']}/" if row.get("project") else " "
    typer.echo(f"  [{row['kind']}, {row['status']}]{project}{row['slug']}")
    if row.get("url"):
        typer.echo(f"  {row['url']}")
    if row.get("why"):
        typer.echo(f"  why: {row['why']}")
    if row.get("fetch_status") == "failed":
        typer.secho("  fetch failed", fg="yellow")


# Options shared by several commands.
Why = Annotated[str | None, typer.Option(help="Why is this worth keeping?")]
Tags = Annotated[list[str] | None, typer.Option("--tag", help="Repeatable")]
Project = Annotated[str | None, typer.Option(help="A project name (default: the global library)")]
Keep = Annotated[
    str | None, typer.Option(help="For a video: transcript, audio or video (default: server's)")
]
MaxHeight = Annotated[
    int | None, typer.Option(help="For --keep video: 360, 480, 720, 1080, 1440 or 2160")
]
Json = Annotated[bool, typer.Option("--json", help="Print JSON (the REST API's shape)")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation")]
Limit = Annotated[int, typer.Option(help="At most this many", min=1, max=200, clamp=True)]
