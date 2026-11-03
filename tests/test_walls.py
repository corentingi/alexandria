"""Sites behind logins or bot checks, and attachment text standing in (ADR-0017)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import respx
from starlette.testclient import TestClient

from alexandria.jobs import Worker
from alexandria.library import Library
from alexandria.models import FetchStatus
from alexandria.urls import canonicalize
from alexandria.walls import detect_block, site_warning
from tests.fixtures import make_pdf
from tests.test_interfaces import TestUi as _InterfaceUi  # aliased: not collected twice
from tests.test_interfaces import auth

HTML = {"content-type": "text/html"}
REDDIT = "https://www.reddit.com/r/ProtonMail/comments/1abc/some_post/"
CHALLENGE = b"""<!DOCTYPE html><html lang="en"><head><title>Reddit</title>
<script nonce="x">document.addEventListener("DOMContentLoaded",async function(){
var e=document.forms[0];e.elements.namedItem("solution").value="1";e.requestSubmit()});
</script></head><body><main><form method="get"><input type="hidden" name="js_challenge"
value="1"><input type="hidden" name="solution"></form></main></body></html>"""


async def stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


class TestSiteWarning:
    def test_known_sites_and_subdomains(self) -> None:
        for url in (REDDIT, "https://old.reddit.com/x", "https://www.linkedin.com/posts/a",
                    "https://x.com/someone/status/1", "https://fr.linkedin.com/in/b"):  # fmt: skip
            warning = site_warning(url)
            assert warning is not None and "attach a PDF" in warning.message, url
        assert site_warning(REDDIT).kind == "bot"  # type: ignore[union-attr]
        assert site_warning("https://www.lemonde.fr/a").kind == "paywall"  # type: ignore[union-attr]

    def test_unknown_and_lookalike_sites(self) -> None:
        assert site_warning("https://blog.example.com/post") is None
        assert site_warning("https://notreddit.com/r/x") is None
        assert site_warning("https://reddit.com.evil.example/") is None

    def test_challenge_params_are_not_part_of_the_link(self) -> None:
        url = REDDIT + "?solution=abc&js_challenge=1&jsc_token=def&jsc_orig_r="
        assert canonicalize(url) == REDDIT.rstrip("/")
        # Without the challenge marker a "solution" parameter is someone's real query.
        assert (
            canonicalize("https://quiz.example/a?solution=3") == "https://quiz.example/a?solution=3"
        )


class TestDetectBlock:
    def test_reddit_and_cloudflare(self) -> None:
        reason = detect_block(CHALLENGE, REDDIT, 0)
        assert reason and reason.startswith("Reddit answered with a bot check")
        cf = b"<html><head><title>Just a moment...</title></head><body></body></html>"
        assert "Cloudflare" in (detect_block(cf, "https://shop.example/", 0) or "")
        login = b"<html><head><title>Sign In | LinkedIn</title></head></html>"
        assert "login page" in (detect_block(login, "https://www.linkedin.com/posts/a", 30) or "")

    def test_real_content_is_not_a_block(self) -> None:
        page = b"<html><head><title>Notes</title></head><body><script>cf-chl</script></body></html>"
        assert detect_block(page, "https://blog.example.com/", 4000) is None
        assert (
            detect_block(b"<html><title>Gardening</title></html>", "https://a.example/", 10) is None
        )


class TestBlockedFetch:
    async def test_bot_check_fails_with_a_clear_message(
        self, library: Library, worker: Worker
    ) -> None:
        with respx.mock() as router:
            router.get(REDDIT).mock(
                return_value=httpx.Response(
                    200, content=CHALLENGE, headers={"content-type": "text/html"}
                )
            )
            result = await library.ingest(REDDIT, why="announcement")
            await worker.drain()
        res = library.get(result.resource.id)
        assert res.fetch_status == FetchStatus.FAILED
        assert res.fetch_error and "bot check" in res.fetch_error and "PDF" in res.fetch_error
        assert res.snapshots == []  # no empty snapshot pretending to be the page
        assert library.index.jobs("failed")[0].attempts == 1  # permanent: not retried

    async def test_403_mentions_the_site(self, library: Library, worker: Worker) -> None:
        url = "https://www.linkedin.com/posts/someone_activity-1"
        with respx.mock() as router:
            router.get(url).mock(return_value=httpx.Response(403))
            result = await library.ingest(url)
            await worker.drain()
        error = library.get(result.resource.id).fetch_error or ""
        assert "HTTP 403" in error and "LinkedIn shows most content only to signed-in" in error


class TestAttachmentText:
    async def test_pdf_attachment_stands_in_for_a_blocked_page(
        self, library: Library, worker: Worker
    ) -> None:
        with respx.mock() as router:
            router.get(REDDIT).mock(
                return_value=httpx.Response(
                    200, content=CHALLENGE, headers={"content-type": "text/html"}
                )
            )
            result = await library.ingest(REDDIT, why="announcement")
            await worker.drain()
        rid = result.resource.id
        body = "Proton will re-interview staff about AI. " + "The thread discusses it. " * 12
        pdf = make_pdf([body, "Comments follow"])
        await library.attach_upload(rid, "Proton post.pdf", stream(pdf))
        await worker.drain()

        res = library.get(rid)
        [attachment] = res.attachments
        assert attachment.extraction == "ok"
        assert attachment.text_path == ".text/Proton_post.pdf.md"
        assert res.title == "Quarterly Report"  # the PDF's title replaces the bot check's
        hits = library.search("re-interview staff")
        assert [row["id"] for _, row in hits] == [rid]
        assert hits[0][0].heading_path == "Proton_post.pdf > Page 1"
        content = library.content(rid)
        assert content["source_role"] == "attachment" and "re-interview" in content["text"]

        before = [(row["id"], h.heading_path) for h, row in library.search("re-interview")]
        assert (await library.reindex()).errors == []
        assert [(row["id"], h.heading_path) for h, row in library.search("re-interview")] == before

        await library.delete_attachment(rid, attachment.path)
        assert library.search("re-interview staff") == []
        text_dir = library.files.attachments_dir(rid) / ".text"
        assert not any(text_dir.iterdir())

    async def test_good_page_keeps_its_own_text(self, library: Library, worker: Worker) -> None:
        note = await library.create_note("Reading", "My own words about herons.")
        await library.attach_upload(note.id, "extra.md", stream(b"# Appendix\n\nEgrets too."))
        await worker.drain()
        assert library.content(note.id)["source_role"] == "note"
        assert [row["id"] for _, row in library.search("egrets")] == [note.id]

    async def test_unsupported_attachment_is_not_queued(self, library: Library) -> None:
        note = await library.create_note("Pics", "x")
        await library.attach_upload(note.id, "photo.png", stream(b"\x89PNG\r\n\x1a\n\x00"))
        assert library.index.jobs("queued") == []


class TestInterfaces:
    def test_add_page_and_api_warn(self, client: TestClient, tokens: dict[str, str]) -> None:
        _InterfaceUi().login(client, tokens["write"])
        live = client.get("/add/check", params={"url": REDDIT})
        assert live.status_code == 200 and "Reddit blocks automated fetches" in live.text
        assert (
            client.get("/add/check", params={"url": "https://blog.example.com/"}).text.strip() == ""
        )
        page = client.get("/add", params={"url": "https://www.linkedin.com/posts/x"}).text
        assert "LinkedIn shows most content only to signed-in users" in page

        res = client.post("/api/v1/resources", json={"url": REDDIT}, headers=auth(tokens["write"]))
        assert "attach a PDF" in res.json()["warning"]
