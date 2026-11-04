from __future__ import annotations

import random
import time

import pytest

from alexandria.chunking import _hard_split, chunk_markdown
from alexandria.config import Settings
from alexandria.fetchers.base import http_client
from alexandria.fetchers.git_repo import GitRepoFetcher
from alexandria.ids import slugify, unique_slug
from alexandria.models import Kind
from alexandria.security import (
    BlockedURL,
    GuardedBackend,
    check_url,
    safe_join,
    sanitize_filename,
    set_resolver,
)
from alexandria.urls import InvalidURL, canonicalize, detect
from tests.conftest import PUBLIC_IP


class TestCanonicalize:
    def test_tracking_params_fragment_port_and_trailing_slash(self) -> None:
        url = "HTTPS://Example.COM:443/Path/?utm_source=x&b=2&a=1&fbclid=z&ref_src=tw#frag"
        assert canonicalize(url) == "https://example.com/Path?a=1&b=2"

    def test_parameters_that_select_content_are_kept(self) -> None:
        a = canonicalize("https://gitlab.example/g/p/-/blob/x.md?ref=v1")
        b = canonicalize("https://gitlab.example/g/p/-/blob/x.md?ref=v2")
        assert a != b and a.endswith("?ref=v1")
        assert canonicalize("https://x.example/?a=2&a=1") == "https://x.example/?a=2&a=1"
        assert canonicalize("https://x.example/?b=1&a=2&a=1") == "https://x.example/?a=2&a=1&b=1"

    def test_root_keeps_its_slash(self) -> None:
        assert canonicalize("http://example.com") == "http://example.com/"
        assert canonicalize("http://example.com:8080/") == "http://example.com:8080/"

    def test_rejects_other_schemes(self) -> None:
        for url in ("ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)"):
            with pytest.raises(InvalidURL):
                canonicalize(url)


class TestDetect:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/Owner/Repo",
            "https://github.com/Owner/Repo/",
            "https://github.com/owner/repo.git",
            "https://github.com/Owner/Repo/tree/main/src",
        ],
    )
    def test_github(self, url: str) -> None:
        d = detect(url)
        assert d.kind == Kind.GIT_REPO
        assert d.forge == "github"
        assert d.canonical_url == "https://github.com/owner/repo"

    def test_github_non_repo_pages_are_articles(self) -> None:
        assert detect("https://github.com/orgs/foo").kind == Kind.ARTICLE

    def test_generic_git(self) -> None:
        d = detect("https://git.example.org/team/tool.git")
        assert d.kind == Kind.GIT_REPO and d.forge is None

    def test_article(self) -> None:
        assert detect("https://example.com/post").kind == Kind.ARTICLE


class TestSlug:
    def test_slugify(self) -> None:
        assert slugify("Éléphant & Château: a story!") == "elephant-chateau-a-story"
        assert slugify("") == "untitled"

    def test_unique(self) -> None:
        taken = {"hello", "hello-2"}
        assert unique_slug("Hello", taken.__contains__) == "hello-3"


class TestSSRF:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/",
            "http://127.0.0.1/",
            "http://192.168.1.1/",
            "http://10.0.0.5/",
            "http://172.16.3.4/",
            "http://100.64.1.1/",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/",
            "http://[fd00::1]/",
            "http://[fe80::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://224.0.0.1/",
            "http://0.0.0.0/",
            "http://intranet.example/",  # resolves to 192.168.1.10
        ],
    )
    async def test_blocks_private(self, url: str) -> None:
        with pytest.raises(BlockedURL):
            await check_url(url)

    async def test_allows_public(self) -> None:
        await check_url("https://example.com/")

    async def test_allowlist(self) -> None:
        await check_url("http://intranet.example/", allow_private_hosts=["intranet.example"])

    async def test_scheme(self) -> None:
        with pytest.raises(BlockedURL):
            await check_url("ftp://example.com/")


class FakeBackend:
    def __init__(self) -> None:
        self.hosts: list[str] = []

    async def connect_tcp(self, host: str, port: int, **kwargs: object) -> str:
        self.hosts.append(host)
        return "stream"


class TestDnsRebinding:
    async def test_connects_to_the_checked_address(self) -> None:
        inner = FakeBackend()
        backend = GuardedBackend(inner, ["intranet.example"])  # type: ignore[arg-type]
        await backend.connect_tcp("blog.example.com", 443)
        await backend.connect_tcp("intranet.example", 80)  # allow-listed: by name
        assert inner.hosts == [PUBLIC_IP, "intranet.example"]

    async def test_a_second_answer_cannot_redirect_the_connection(self) -> None:
        answers = iter([[PUBLIC_IP], ["127.0.0.1"]])

        async def rebinding(host: str, port: int) -> list[str]:
            return next(answers)

        previous = set_resolver(rebinding)
        try:
            await check_url("https://rebinding.example/")  # first answer: public
            inner = FakeBackend()
            with pytest.raises(BlockedURL, match="non-public"):
                await GuardedBackend(inner, []).connect_tcp("rebinding.example", 443)  # type: ignore[arg-type]
            assert inner.hosts == []
        finally:
            set_resolver(previous)

    def test_http_client_uses_the_guarded_backend(self, settings: Settings) -> None:
        client = http_client(settings)
        pool = client._transport._pool  # type: ignore[attr-defined]
        assert isinstance(pool._network_backend, GuardedBackend)

    async def test_git_clone_is_pinned(self, settings: Settings) -> None:
        fetcher = GitRepoFetcher(settings, ("https", "file"))
        assert await fetcher._guard("https://github.com/a/b.git") == f"github.com:443:{PUBLIC_IP}"
        assert await fetcher._guard("file:///tmp/repo") is None


class TestPaths:
    def test_sanitize(self) -> None:
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("my report (final).pdf") == "my_report_final_.pdf"
        assert sanitize_filename("...") == "file"
        assert sanitize_filename("C:\\Users\\x\\evil.exe") == "evil.exe"

    def test_safe_join(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        assert safe_join(tmp_path, "a", "b") == (tmp_path / "a" / "b").resolve()
        with pytest.raises(PermissionError):
            safe_join(tmp_path, "..", "x")


def _reference_split(paragraph: str, max_chars: int) -> list[str]:
    """The original slicing implementation, kept to check the index walk against."""
    if len(paragraph) <= max_chars:
        return [paragraph]
    parts, rest = [], paragraph
    while len(rest) > max_chars:
        cut = rest.rfind(" ", 0, max_chars)
        cut = max_chars if cut <= 0 else cut
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [*parts, rest] if rest else parts


class TestChunking:
    def test_hard_split_matches_the_original(self) -> None:
        rng = random.Random(7)
        for _ in range(300):
            text = "".join(rng.choice("ab  \t\nx") for _ in range(rng.randint(0, 400)))
            limit = rng.randint(1, 40)
            assert _hard_split(text, limit) == _reference_split(text, limit), (text, limit)

    def test_hard_split_is_linear(self) -> None:
        text = "word " * 2_000_000  # 10 MB, no blank line
        started = time.perf_counter()
        parts = _hard_split(text, 2000)
        assert time.perf_counter() - started < 2  # the slicing version took about 3 s
        assert "".join(parts).replace(" ", "") == text.replace(" ", "")

    def test_headings_and_paths(self) -> None:
        text = "intro\n\n# Title\n\npara\n\n## Sub\n\nmore\n\n# Other\n\nend"
        chunks = chunk_markdown(text)
        assert [c.heading_path for c in chunks] == ["", "Title", "Title > Sub", "Other"]
        assert chunks[2].text.startswith("## Sub")

    def test_code_fences_are_not_headings(self) -> None:
        chunks = chunk_markdown("# A\n\n```\n# not a heading\n```\n")
        assert len(chunks) == 1

    def test_long_sections_split_on_paragraphs(self) -> None:
        para = "word " * 300  # ~1500 chars
        chunks = chunk_markdown(f"# H\n\n{para}\n\n{para}\n\n{para}")
        assert len(chunks) == 3
        assert all(len(c.text) <= 2000 for c in chunks)
        assert all(c.heading_path == "H" for c in chunks)

    def test_huge_paragraph(self) -> None:
        chunks = chunk_markdown("x " * 5000)
        assert all(len(c.text) <= 2000 for c in chunks)

    def test_stable(self) -> None:
        text = "# A\n\n" + "b " * 3000
        assert chunk_markdown(text) == chunk_markdown(text)


def test_flash_redirect_keeps_the_fragment_last() -> None:
    from alexandria.ui.routes import back

    res = back("/settings#library", notice="Export deleted.")
    assert res.headers["location"] == "/settings?notice=Export+deleted.#library"
    assert (
        back("/r/x?edit=note#note", error="no").headers["location"]
        == "/r/x?edit=note&error=no#note"
    )
    assert back("/settings#tokens").headers["location"] == "/settings#tokens"
