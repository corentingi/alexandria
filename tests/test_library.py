from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest
import respx

from alexandria.jobs import Worker
from alexandria.library import InvalidInput, Library, Update
from alexandria.models import FetchStatus, Kind, Status
from alexandria.storage.index import SearchFilters
from tests.conftest import ARTICLE_HTML

ARTICLE_URL = "https://blog.example.com/posts/on-building-libraries?utm_source=hn"


def mock_article(
    router: respx.MockRouter, url: str = ARTICLE_URL, html: str = ARTICLE_HTML
) -> None:
    router.get(url).mock(
        return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
    )


async def ingest_article(library: Library, worker: Worker, why: str | None = "testing") -> str:
    with respx.mock(assert_all_called=False) as router:
        mock_article(router)
        result = await library.ingest(ARTICLE_URL, why=why, tags=["Books", "history"])
        await worker.drain()
    return result.resource.id


class TestArticle:
    async def test_ingest_fetch_index(self, library: Library, worker: Worker) -> None:
        with respx.mock() as router:
            mock_article(router)
            result = await library.ingest(ARTICLE_URL, why="reference on libraries")
            assert result.duplicate is False
            assert result.resource.fetch_status == FetchStatus.PENDING
            assert result.resource.status == Status.KEPT
            await worker.drain()

        res = library.get(result.resource.id)
        assert res.fetch_status == FetchStatus.READY, res.fetch_error
        assert res.title == "On Building Libraries"
        assert res.slug == "on-building-libraries"
        assert res.canonical_url == "https://blog.example.com/posts/on-building-libraries"
        snap = library.files.snapshot_dir(res.id, res.current_snapshot or "")
        assert {p.name for p in snap.iterdir()} == {"page.html", "content.md", "fetch.json"}
        assert res.meta["extraction"] == "ok"
        assert res.meta["language"] == "en"

        hits = library.search("papyrus scrolls")
        assert [row["id"] for _, row in hits] == [res.id]
        assert "\x02" in (hits[0][0].snippet or "")
        assert library.get(res.slug).id == res.id

    async def test_duplicate_returns_existing(self, library: Library, worker: Worker) -> None:
        rid = await ingest_article(library, worker)
        again = await library.ingest(
            "https://blog.example.com/posts/on-building-libraries/#comments"
        )
        assert again.duplicate is True
        assert again.resource.id == rid
        assert again.resource.why == "testing"  # never overwritten

    async def test_ssrf_rejected(self, library: Library, worker: Worker) -> None:
        for url in ("http://192.168.1.1/", "http://localhost/"):
            result = await library.ingest(url)
            await worker.drain()
            res = library.get(result.resource.id)
            assert res.fetch_status == FetchStatus.FAILED
            assert "SSRF" in (res.fetch_error or "")
        assert all(j.status == "failed" and j.attempts == 1 for j in library.index.jobs())

    async def test_redirect_to_private_is_blocked(self, library: Library, worker: Worker) -> None:
        with respx.mock() as router:
            router.get("https://public.example/x").mock(
                return_value=httpx.Response(302, headers={"location": "http://rebind.example/"})
            )
            result = await library.ingest("https://public.example/x")
            await worker.drain()
        res = library.get(result.resource.id)
        assert res.fetch_status == FetchStatus.FAILED and "SSRF" in (res.fetch_error or "")

    async def test_retry_then_fail(self, library: Library, worker: Worker) -> None:
        with respx.mock() as router:
            route = router.get("https://flaky.example/").mock(return_value=httpx.Response(503))
            result = await library.ingest("https://flaky.example/")
            await worker.drain()
        assert route.call_count == 3
        assert library.get(result.resource.id).fetch_status == FetchStatus.FAILED

    async def test_404_is_permanent(self, library: Library, worker: Worker) -> None:
        with respx.mock() as router:
            route = router.get("https://gone.example/").mock(return_value=httpx.Response(404))
            await library.ingest("https://gone.example/")
            await worker.drain()
        assert route.call_count == 1

    async def test_size_cap(self, library: Library, worker: Worker) -> None:
        library.settings.fetch.max_bytes = 100
        with respx.mock() as router:
            router.get("https://big.example/").mock(
                return_value=httpx.Response(200, content=b"x" * 1000)
            )
            result = await library.ingest("https://big.example/")
            await worker.drain()
        res = library.get(result.resource.id)
        assert res.fetch_status == FetchStatus.FAILED and "cap" in (res.fetch_error or "")

    async def test_non_html_saved_as_original(self, library: Library, worker: Worker) -> None:
        with respx.mock() as router:
            router.get("https://files.example/paper.pdf").mock(
                return_value=httpx.Response(
                    200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}
                )
            )
            result = await library.ingest("https://files.example/paper.pdf")
            await worker.drain()
        res = library.get(result.resource.id)
        assert res.fetch_status == FetchStatus.READY
        assert res.artifact("original") is not None
        assert res.artifact("original").path == "original.pdf"  # type: ignore[union-attr]
        assert res.meta["content_type"] == "application/pdf"

    async def test_refetch_keeps_old_snapshot(self, library: Library, worker: Worker) -> None:
        rid = await ingest_article(library, worker)
        first = library.get(rid).current_snapshot
        with respx.mock() as router:
            mock_article(router, html=ARTICLE_HTML.replace("Papyrus", "Parchment"))
            await library.refetch(rid)
            await worker.drain()
        res = library.get(rid)
        assert len(res.snapshots) == 2
        assert res.current_snapshot != first
        assert library.files.snapshot_dir(rid, first or "").exists()
        assert [r["id"] for _, r in library.search("parchment")] == [rid]
        assert library.search("papyrus") == []

    async def test_human_title_survives_refetch(self, library: Library, worker: Worker) -> None:
        rid = await ingest_article(library, worker)
        await library.update(rid, Update(title="My own title"))
        with respx.mock() as router:
            mock_article(router)
            await library.refetch(rid)
            await worker.drain()
        assert library.get(rid).title == "My own title"

    async def test_possible_duplicate(self, library: Library, worker: Worker) -> None:
        rid = await ingest_article(library, worker)
        with respx.mock() as router:
            mock_article(router, url="https://mirror.example/copy")
            other = await library.ingest("https://mirror.example/copy")
            await worker.drain()
        assert library.get(other.resource.id).meta["possible_duplicate_of"] == rid


class TestInboxAndUpdate:
    async def test_inbox_then_kept(self, library: Library, worker: Worker) -> None:
        rid = await ingest_article(library, worker, why=None)
        assert library.get(rid).status == Status.INBOX
        inbox = library.search("", SearchFilters(status="inbox"))
        assert [r["id"] for _, r in inbox] == [rid]
        res = await library.update(rid, Update(why="Because it explains capture"))
        assert res.status == Status.KEPT
        assert library.search("", SearchFilters(status="inbox")) == []

    async def test_cannot_keep_without_why(self, library: Library, worker: Worker) -> None:
        rid = await ingest_article(library, worker, why=None)
        with pytest.raises(InvalidInput):
            await library.update(rid, Update(status=Status.KEPT))

    async def test_archived_excluded_by_default(self, library: Library, worker: Worker) -> None:
        rid = await ingest_article(library, worker)
        await library.update(rid, Update(status=Status.ARCHIVED))
        assert library.search("papyrus") == []
        assert len(library.search("papyrus", SearchFilters(status="archived"))) == 1

    async def test_tags_filter_and_list(self, library: Library, worker: Worker) -> None:
        rid = await ingest_article(library, worker)
        assert library.get(rid).tags == ["books", "history"]
        assert len(library.search("", SearchFilters(tags=["books", "history"]))) == 1
        assert library.search("", SearchFilters(tags=["books", "nope"])) == []
        assert dict(library.tags()) == {"books": 1, "history": 1}


class TestNotes:
    async def test_note_is_indexed_immediately(self, library: Library) -> None:
        note = await library.create_note(
            "Meeting notes", "# Decisions\n\nWe chose SQLite over Postgres.", tags=["db"]
        )
        assert note.kind == Kind.NOTE and note.status == Status.INBOX
        assert [r["id"] for _, r in library.search("sqlite")] == [note.id]
        content = library.content(note.slug)
        assert "SQLite" in content["text"]
        await library.update(note.id, Update(notes="Now about DuckDB"))
        assert [r["id"] for _, r in library.search("duckdb")] == [note.id]

    async def test_title_weighs_more_than_content(self, library: Library) -> None:
        a = await library.create_note("Unrelated", "kangaroo appears in the body only")
        b = await library.create_note("Kangaroo facts", "marsupials")
        assert [r["id"] for _, r in library.search("kangaroo")] == [b.id, a.id]


class TestReindex:
    async def test_delete_db_and_reindex_is_identical(
        self, library: Library, worker: Worker
    ) -> None:
        await ingest_article(library, worker)
        await library.create_note("Kangaroo facts", "Marsupials of the libraries", tags=["zoo"])
        await library.create_note("Libraries", "about libraries", why="ref")
        queries = ["libraries", "kangaroo", "papyrus", ""]

        def snapshot() -> list[list[tuple[str, str | None]]]:
            return [[(r["id"], h.snippet) for h, r in library.search(q)] for q in queries]

        before = snapshot()
        library.index.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(library.settings.db_path) + suffix).unlink(missing_ok=True)
        library.__init__(library.settings, library.fetchers)  # type: ignore[misc]
        report = await library.reindex()
        assert report.errors == [] and report.indexed == 3
        assert snapshot() == before

    async def test_reports_invalid_files(self, library: Library) -> None:
        note = await library.create_note("A", "b")
        bad = library.settings.resources_dir / "0192f3a4-7c1e-7b2a-9d3e-5f6a7b8c9d0e"
        bad.mkdir()
        (bad / "resource.md").write_text("---\nid: nope\n---\n")
        report = await library.reindex()
        assert report.indexed == 1 and len(report.errors) == 1
        assert library.get(note.id)


class TestAttachments:
    async def test_upload(self, library: Library) -> None:
        note = await library.create_note("A", "b")

        async def body():  # type: ignore[no-untyped-def]
            yield b"hello "
            yield b"world"

        att = await library.attach_upload(note.id, "../../evil name.txt", body())
        assert att.path == "evil_name.txt" and att.size == 11
        att2 = await library.attach_upload(note.id, "evil name.txt", body())
        assert att2.path == "evil_name-2.txt"
        path = library.files.file_path(note.id, "attachments/evil_name.txt")
        assert path.read_bytes() == b"hello world"
        with pytest.raises((FileNotFoundError, PermissionError)):
            library.files.file_path(note.id, "../../alexandria.db")
        with pytest.raises(FileNotFoundError):
            library.files.file_path(note.id, "resource.md")

    async def test_from_url(self, library: Library, worker: Worker) -> None:
        note = await library.create_note("A", "b")
        with respx.mock() as router:
            router.get("https://dl.example/release.zip").mock(
                return_value=httpx.Response(
                    200, content=b"PK..", headers={"content-type": "application/zip"}
                )
            )
            await library.attach_url(note.id, "https://dl.example/release.zip", "me")
            await worker.drain()
        res = library.get(note.id)
        assert res.attachments[0].path == "release.zip"
        assert res.attachments[0].origin_url == "https://dl.example/release.zip"
        assert res.attachments[0].added_by == "me"


# -- git ------------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    ).stdout


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "src-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("# Demo\n\nA demo repository about quokkas.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    for n in range(5):
        (repo / f"f{n}.txt").write_text(str(n))
        git(repo, "add", ".")
        git(repo, "commit", "-m", f"c{n}")
    git(repo, "tag", "v1.0")
    git(repo, "branch", "feature")
    git(repo, "checkout", "feature")
    (repo / "feat.txt").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feature")
    git(repo, "checkout", "main")
    return repo


class TestGit:
    async def test_full_and_shallow(
        self, library: Library, worker: Worker, source_repo: Path, tmp_path: Path
    ) -> None:
        from alexandria.fetchers.git_repo import GitRepoFetcher

        fetcher = library.fetchers[Kind.GIT_REPO]
        assert isinstance(fetcher, GitRepoFetcher)
        note = await library.create_note("placeholder", "x")
        res = library.get(note.id)
        res = res.model_copy(update={"kind": Kind.GIT_REPO, "source_url": f"file://{source_repo}"})

        staging = tmp_path / "stage-full"
        staging.mkdir()
        snap = await fetcher.fetch(res, staging)
        repo_art = next(a for a in snap.artifacts if a.role == "repository")
        extra = repo_art.model_extra or {}
        assert extra["history"] == "full" and extra["commit_count"] == 6
        assert snap.text_role == "readme"
        assert "quokkas" in (staging / "README.md").read_text()

        # Restore procedure from the README: extract, then clone.
        out = tmp_path / "restore"
        out.mkdir()
        with tarfile.open(staging / "repository.git.tar.gz") as tar:
            tar.extractall(out, filter="data")
        git(out, "clone", "repository.git", "checkout")
        checkout = out / "checkout"
        branches = git(checkout, "branch", "-r")
        assert "origin/feature" in branches and "origin/main" in branches
        assert git(checkout, "tag").split() == ["v1.0"]
        assert git(checkout, "rev-list", "--count", "--all").strip() == "7"

        # Above the threshold: shallow, default branch only.
        library.settings.git.shallow_depth = 2
        size_kb = (library.settings.git.full_history_max_mb + 1) * 1024
        staging2 = tmp_path / "stage-shallow"
        (staging2 / "work").mkdir(parents=True)
        clone = await fetcher._clone(f"file://{source_repo}", staging2 / "work", size_kb)
        assert clone.history == "shallow" and clone.depth == 2
        assert (clone.path / "shallow").exists()

    async def test_github_metadata(
        self, library: Library, worker: Worker, source_repo: Path
    ) -> None:
        fetcher = library.fetchers[Kind.GIT_REPO]
        real_clone = fetcher._clone  # type: ignore[attr-defined]

        async def local_clone(url: str, work: Path, size_kb: int | None):  # type: ignore[no-untyped-def]
            assert url == "https://github.com/Owner/Repo.git"
            assert size_kb == 10
            return await real_clone(f"file://{source_repo}", work, size_kb)

        fetcher._clone = local_clone  # type: ignore[attr-defined]
        with respx.mock() as router:
            router.get("https://api.github.com/repos/owner/repo").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "full_name": "Owner/Repo",
                        "description": "A thing",
                        "topics": ["mcp"],
                        "license": {"spdx_id": "MIT"},
                        "stargazers_count": 5,
                        "size": 10,
                        "default_branch": "main",
                        "language": "Python",
                        "archived": False,
                        "pushed_at": "2026-01-01T00:00:00Z",
                        "clone_url": "https://github.com/Owner/Repo.git",
                    },
                )
            )
            router.get("https://api.github.com/repos/owner/repo/readme").mock(
                return_value=httpx.Response(200, text="# Repo\n\nAbout wombats.")
            )
            result = await library.ingest("https://github.com/Owner/Repo/", why="x")
            assert result.resource.slug == "owner-repo"
            await worker.drain()
        res = library.get(result.resource.id)
        assert res.fetch_status == FetchStatus.READY, res.fetch_error
        assert res.title == "Owner/Repo"
        assert res.meta["license"] == "MIT" and res.meta["topics"] == ["mcp"]
        roles = {a.role for a in res.snapshot().artifacts}  # type: ignore[union-attr]
        assert roles == {"repository", "readme", "github", "fetch"}
        assert [r["id"] for _, r in library.search("wombats")] == [res.id]
        assert [r["id"] for _, r in library.search("mcp")] == [res.id]  # topics are searchable

    async def test_github_404_is_permanent(self, library: Library, worker: Worker) -> None:
        with respx.mock() as router:
            router.get("https://api.github.com/repos/a/b").mock(return_value=httpx.Response(404))
            result = await library.ingest("https://github.com/a/b")
            await worker.drain()
        res = library.get(result.resource.id)
        assert res.fetch_status == FetchStatus.FAILED and "not found" in (res.fetch_error or "")
