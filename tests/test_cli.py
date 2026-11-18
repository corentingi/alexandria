"""The command line, run in-process on the test data directory (no server)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alexandria.cli import app
from alexandria.config import Settings
from alexandria.jobs import Worker
from alexandria.library import Library
from tests.conftest import URL

VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
PRIVATE_VIDEO = "https://www.youtube.com/watch?v=privateabcd"


@pytest.fixture
def cli(settings: Settings) -> CliRunner:
    """The settings fixture points ALEXANDRIA_DATA_DIR at the test's data directory."""
    return CliRunner()


def ok(runner: CliRunner, *args: str, stdin: str | None = None) -> str:
    result = runner.invoke(app, list(args), input=stdin)
    assert result.exit_code == 0, (args, result.output)
    return result.output


def refused(runner: CliRunner, *args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 1, (args, result.output)
    return result.output


def drain(settings: Settings) -> None:
    """Run the queued jobs, as the server's worker would."""
    library = Library(settings)
    try:
        asyncio.run(Worker(library).drain())
    finally:
        library.close()


class TestCli:
    def test_find_and_read(self, cli: CliRunner) -> None:
        assert "Saved" in ok(cli, "add", URL, "--why", "a good read", "--tag", "web")
        assert "Already saved" in ok(cli, "add", URL)
        ok(cli, "note", "Shopping", "--body-file", "-", "--tag", "home", stdin="# Eggs\n\nsix")

        listed = ok(cli, "list")
        assert "Shopping" in listed and "[article, kept]" in listed
        rows = json.loads(ok(cli, "list", "--kind", "note", "--json"))
        assert [r["title"] for r in rows] == ["Shopping"]
        assert "Eggs" in ok(cli, "content", rows[0]["slug"])
        assert "Shopping" in ok(cli, "search", "eggs")

        saved = json.loads(ok(cli, "lookup", URL, "--json"))
        assert saved["saved"] and saved["why"] == "a good read"
        assert "Not saved." in ok(cli, "lookup", "https://elsewhere.example/x")
        detail = json.loads(ok(cli, "show", saved["slug"], "--json"))
        assert detail["tags"] == ["web"] and detail["fetch_status"] == "pending"
        assert "a good read" in ok(cli, "show", saved["id"])
        assert "web" in ok(cli, "tags")
        assert "no resource" in refused(cli, "show", "nope").lower()

    def test_edit(self, cli: CliRunner) -> None:
        ok(cli, "add", URL, "--tag", "a", "--tag", "b")
        slug = json.loads(ok(cli, "lookup", URL, "--json"))["slug"]
        ok(cli, "edit", slug, "--why", "worth it", "--tag", "c", "--untag", "a",
           "--project", "research", "--title", "Mine")  # fmt: skip
        r = json.loads(ok(cli, "show", slug, "--json"))
        assert r["tags"] == ["b", "c"] and r["status"] == "kept" and r["title"] == "Mine"
        assert r["project"] == "research" and "research" in ok(cli, "projects")
        ok(cli, "edit", slug, "--project", "", "--status", "archived")
        r = json.loads(ok(cli, "show", slug, "--json"))
        assert "project" not in r  # compact: no project
        assert r["status"] == "archived" and r["scope"] == "global"
        assert "nothing to change" in refused(cli, "edit", slug)
        assert "only a video" in refused(cli, "edit", slug, "--keep", "transcript")
        assert "Nothing." in ok(cli, "list")  # archived is left out
        assert "Mine" in ok(cli, "list", "--status", "archived")

    def test_refetch(self, cli: CliRunner, settings: Settings) -> None:
        ok(cli, "add", VIDEO, "--why", "zoo")
        ok(cli, "note", "A note")
        pending = json.loads(ok(cli, "lookup", VIDEO, "--json"))["slug"]  # not fetched yet
        assert "already queued" in refused(cli, "refetch", pending)  # exit 1: named, skipped
        drain(settings)

        assert "name resources" in refused(cli, "refetch")
        assert "not both" in refused(cli, "refetch", "x", "--kind", "media")
        out = ok(cli, "refetch", "--kind", "media", "--yes")
        assert "1 fetch(es) queued, 0 skipped" in out
        drain(settings)
        # Notes have nothing to fetch: --all leaves them out instead of failing on them.
        assert "1 fetch(es) queued, 0 skipped" in ok(cli, "refetch", "--all", "--yes")
        assert "a note has no source" in refused(cli, "refetch", "a-note")

    def test_video_options_and_jobs(self, cli: CliRunner, settings: Settings) -> None:
        assert "only the transcripts" in refused(cli, "add", VIDEO, "--keep", "audio")
        assert "needs --keep video" in refused(cli, "edit", "x", "--max-height", "720")
        ok(cli, "add", VIDEO)
        ok(cli, "add", PRIVATE_VIDEO)
        drain(settings)
        ok(cli, "edit", "me-at-the-zoo", "--keep", "transcript")
        assert "keeps" in ok(cli, "show", "me-at-the-zoo")

        rows = json.loads(ok(cli, "list", "--fetch-status", "failed", "--json"))
        assert [r["fetch_status"] for r in rows] == ["failed"]
        failed = ok(cli, "jobs", "list", "--status", "failed")
        assert "Private video" in failed
        job_id = failed.split()[0]
        assert json.loads(ok(cli, "jobs", "show", job_id))["status"] == "failed"
        assert "give a job id, or --failed" in refused(cli, "jobs", "retry")
        assert "1 job(s) queued again" in ok(cli, "jobs", "retry", "--failed")
        assert "no failed job" in refused(cli, "jobs", "retry", job_id)
        assert "queued" in ok(cli, "stats")

    def test_files(self, cli: CliRunner, settings: Settings, tmp_path: Path) -> None:
        ok(cli, "add", VIDEO)
        drain(settings)
        ok(cli, "refetch", "me-at-the-zoo")
        drain(settings)
        snapshots = ok(cli, "snapshot", "list", "me-at-the-zoo").splitlines()
        assert len(snapshots) == 2 and snapshots[1].endswith("(current)")
        ok(cli, "snapshot", "delete", "me-at-the-zoo", snapshots[0].split()[0], "--yes")
        assert "only snapshot" in refused(
            cli, "snapshot", "delete", "me-at-the-zoo", snapshots[1].split()[0], "--yes"
        )

        doc = tmp_path / "slides.txt"
        doc.write_text("the slides")
        assert "Attached slides.txt" in ok(cli, "attachment", "add", "me-at-the-zoo", str(doc))
        assert "no such file" in refused(cli, "attachment", "add", "me-at-the-zoo", "nope.txt")
        assert "Extraction queued" in ok(
            cli, "attachment", "extract", "me-at-the-zoo", "attachments/slides.txt"
        )
        ok(cli, "attachment", "delete", "me-at-the-zoo", "slides.txt", "--yes")
        assert "attachment" not in ok(cli, "show", "me-at-the-zoo")
        ok(cli, "delete", "me-at-the-zoo", "--yes")
        assert "Nothing." in ok(cli, "list")


class TestCliReview:
    """Edge cases found in review: each would have misbehaved."""

    def test_empty_filters_never_mean_everything(self, cli: CliRunner) -> None:
        ok(cli, "note", "A note")
        for args in (["--tag", ""], ["--tag", "  "], ["--project", ""], ["--site", " "]):
            assert "empty --tag, --project or --site" in refused(cli, "refetch", *args, "--yes")
            assert "empty" in refused(cli, "snapshot", "prune", *args, "--yes")

    def test_confirmation(self, cli: CliRunner, settings: Settings) -> None:
        ok(cli, "add", VIDEO)
        drain(settings)
        result = cli.invoke(app, ["refetch", "--kind", "media"], input="n\n")
        assert result.exit_code == 1 and "Aborted" in result.output
        assert "No jobs." not in ok(cli, "jobs", "list", "--status", "done")
        assert "No jobs." in ok(cli, "jobs", "list", "--status", "queued")

    def test_video_height_rules(
        self, cli: CliRunner, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALEXANDRIA_MEDIA__DOWNLOADS", "true")
        assert "needs --keep video" in refused(cli, "add", VIDEO, "--max-height", "720")
        ok(cli, "add", VIDEO, "--keep", "video", "--max-height", "480")
        slug = json.loads(ok(cli, "lookup", VIDEO, "--json"))["slug"]

        def media() -> object:
            return json.loads(ok(cli, "show", slug, "--json"))["media"]

        assert media() == {"keep": "video", "max_height": 480}
        ok(cli, "edit", slug, "--keep", "video")  # the cap stays
        assert media() == {"keep": "video", "max_height": 480}
        ok(cli, "edit", slug, "--keep", "audio")  # a height means nothing for audio
        assert media() == {"keep": "audio", "max_height": None}

    def test_clean_errors(self, cli: CliRunner, settings: Settings, tmp_path: Path) -> None:
        ok(cli, "note", "N")
        assert "No such file" in refused(cli, "edit", "n", "--notes-file", str(tmp_path / "x"))
        assert "not both" in refused(cli, "edit", "n", "--notes", "a", "--notes-file", "-")
        assert refused(cli, "upload", str(tmp_path / "missing.pdf"))
        assert cli.invoke(app, ["import", str(tmp_path / "missing.txt")]).exit_code == 2
        assert "no chunk 7" in refused(cli, "content", "n", "--chunk", "7")
        assert "no snapshot" in refused(cli, "snapshot", "delete", "n", "nope")
        assert "no attachment" in refused(cli, "attachment", "delete", "n", "nope.pdf")
        assert "no export" in refused(cli, "library", "delete-export", "nope")

    def test_retry_refused_exits_1(self, cli: CliRunner, settings: Settings) -> None:
        ok(cli, "add", PRIVATE_VIDEO)
        drain(settings)
        job_id = ok(cli, "jobs", "list", "--status", "failed").split()[0]
        slug = json.loads(ok(cli, "lookup", PRIVATE_VIDEO, "--json"))["slug"]
        ok(cli, "refetch", slug)  # a new fetch is pending
        assert "already queued" in refused(cli, "jobs", "retry", job_id)


class TestSnapshotPrune:
    def test_prune_one_and_by_site(self, cli: CliRunner, settings: Settings) -> None:
        ok(cli, "add", VIDEO)
        ok(cli, "add", "https://vimeo.com/123456")
        drain(settings)
        for _ in range(2):
            ok(cli, "refetch", "--all", "--yes")
            drain(settings)
        assert len(ok(cli, "snapshot", "list", "me-at-the-zoo").splitlines()) == 3
        assert [
            r["slug"] for r in json.loads(ok(cli, "list", "--site", "youtube.com", "--json"))
        ] == ["me-at-the-zoo"]
        result = cli.invoke(app, ["snapshot", "prune", "--site", "youtube.com"], input="y\n")
        assert result.exit_code == 0 and "Deleted 2 snapshot(s)" in result.output
        lines = ok(cli, "snapshot", "list", "me-at-the-zoo").splitlines()
        assert len(lines) == 1 and lines[0].endswith("(current)")
        vimeo = json.loads(ok(cli, "lookup", "https://vimeo.com/123456", "--json"))["slug"]
        assert len(ok(cli, "snapshot", "list", vimeo).splitlines()) == 3  # other site kept
        assert "Deleted 2 snapshot(s)" in ok(cli, "snapshot", "prune", vimeo, "--yes")
        assert "No old snapshots." in ok(cli, "snapshot", "prune", "--all", "--yes")
