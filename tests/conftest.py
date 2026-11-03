from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from alexandria import security
from alexandria.app import create_app
from alexandria.auth import ScopeLevel, TokenStore
from alexandria.config import Settings, load_settings
from alexandria.fetchers.article import ArticleFetcher
from alexandria.fetchers.git_repo import GitRepoFetcher
from alexandria.jobs import Worker
from alexandria.library import Library
from alexandria.models import Kind

URL = "https://blog.example.com/posts/on-building-libraries"
PUBLIC_IP = "93.184.216.34"
# Hostnames the fake resolver maps to private addresses (to exercise the SSRF guard).
PRIVATE_HOSTS = {"intranet.example": "192.168.1.10", "rebind.example": "127.0.0.1"}


async def fake_resolver(host: str, port: int) -> list[str]:
    if host in PRIVATE_HOSTS:
        return [PRIVATE_HOSTS[host]]
    return [PUBLIC_IP]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The suite never touches DNS or the network (acceptance criterion 10)."""
    previous = security.set_resolver(fake_resolver)

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network access in tests")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield
    security.set_resolver(previous)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ALEXANDRIA_DATA_DIR", str(tmp_path / "data"))
    for key in list(__import__("os").environ):
        if key.startswith("ALEXANDRIA_") and key != "ALEXANDRIA_DATA_DIR":
            monkeypatch.delenv(key)
    s = load_settings()
    s.worker.backoff_s = [0, 0, 0]
    return s


@pytest.fixture
async def library(settings: Settings) -> AsyncIterator[Library]:
    lib = Library(
        settings,
        fetchers={
            Kind.ARTICLE: ArticleFetcher(settings),
            Kind.GIT_REPO: GitRepoFetcher(settings, allowed_protocols=("https", "file")),
        },
    )
    yield lib
    lib.close()


@pytest.fixture
def worker(library: Library) -> Worker:
    return Worker(library)


ARTICLE_HTML = """<!doctype html>
<html lang="en"><head><title>On Building Libraries | Example Blog</title>
<meta name="author" content="Ada Lovelace"></head>
<body><nav>Home About</nav><article>
<h1>On Building Libraries</h1>
<p>The ancient library of Alexandria copied every book found on the ships arriving in its
port. This essay discusses how personal knowledge bases can follow the same principle:
capture first, organize later, and always record why something was kept.</p>
<h2>Capture</h2>
<p>Capturing should be cheap. A bookmarklet, a command line, an agent calling a tool: every
path must end in the same archive, with the same guarantees about durability and search.
Papyrus scrolls were copied by hand; we copy with HTTP requests and Markdown.</p>
<h2>Retrieval</h2>
<p>Retrieval depends on good indexing. Full-text search with stemming and ranking goes a
long way before anyone needs embeddings or vector databases for a personal collection.</p>
</article><footer>Copyright</footer></body></html>
"""


@pytest.fixture
def tokens(settings: Settings) -> dict[str, str]:
    store = TokenStore(settings.tokens_path)
    return {level.label: store.create(level.label, level) for level in ScopeLevel}


@pytest.fixture
def client(settings: Settings, tokens: dict[str, str]) -> Iterator[TestClient]:
    settings.worker.poll_interval_s = 0.05
    with respx.mock(assert_all_called=False) as router:
        router.route(host="testserver").pass_through()
        router.get(URL).mock(
            return_value=httpx.Response(
                200, text=ARTICLE_HTML, headers={"content-type": "text/html"}
            )
        )
        with TestClient(create_app(settings)) as c:
            yield c
