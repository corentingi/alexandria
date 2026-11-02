"""REST API, MCP and web UI, through the real ASGI app (lifespan, worker and all)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from alexandria.app import create_app
from alexandria.auth import ScopeLevel, TokenStore
from alexandria.config import Settings
from tests.conftest import ARTICLE_HTML

URL = "https://blog.example.com/posts/on-building-libraries"


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


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wait_ready(client: TestClient, ref: str, token: str, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data: dict[str, Any] = client.get(f"/api/v1/resources/{ref}", headers=auth(token)).json()
        if data["fetch_status"] in ("ready", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError("fetch did not finish")


class TestRest:
    def test_health_needs_no_auth(self, client: TestClient) -> None:
        assert client.get("/healthz").json()["ok"] is True

    def test_auth_required(self, client: TestClient) -> None:
        assert client.get("/api/v1/search").status_code == 401
        assert client.get("/api/v1/search", headers=auth("alx_wrong")).status_code == 401

    def test_ingest_flow(self, client: TestClient, tokens: dict[str, str]) -> None:
        w, r = tokens["write"], tokens["read"]
        res = client.post("/api/v1/resources", json={"url": URL, "tags": ["x"]}, headers=auth(w))
        assert res.status_code == 202
        body = res.json()
        assert body["status"] == "inbox" and body["duplicate"] is False
        data = wait_ready(client, body["id"], r)
        assert data["fetch_status"] == "ready"
        roles = {a["role"] for a in data["snapshots"][0]["artifacts"]}
        assert roles == {"page", "content", "fetch"}
        assert data["chunks"]

        found = client.get("/api/v1/search", params={"q": "papyrus"}, headers=auth(r)).json()
        assert [x["id"] for x in found["results"]] == [body["id"]]
        assert "**Papyrus**" in found["results"][0]["snippet"]

        dup = client.post(
            "/api/v1/resources", json={"url": URL + "/?utm_source=x"}, headers=auth(w)
        )
        assert dup.status_code == 200 and dup.json()["duplicate"] is True

        patched = client.patch(
            f"/api/v1/resources/{data['slug']}", json={"why": "great essay"}, headers=auth(w)
        ).json()
        assert patched["status"] == "kept"

        content = client.get(
            f"/api/v1/resources/{body['id']}/content", params={"max_chars": 200}, headers=auth(r)
        ).json()
        assert content["source_url"] == URL and len(content["text"]) == 200
        assert content["next_offset"] == 200

        path = data["snapshots"][0]["artifacts"][0]["path"]
        dl = client.get(f"/api/v1/resources/{body['id']}/files/{path}", headers=auth(r))
        assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
        bad = client.get(f"/api/v1/resources/{body['id']}/files/../../tokens.yaml", headers=auth(r))
        assert bad.status_code == 404

    def test_scopes(self, client: TestClient, tokens: dict[str, str]) -> None:
        r, w, a = tokens["read"], tokens["write"], tokens["admin"]
        assert (
            client.post("/api/v1/resources", json={"url": URL}, headers=auth(r)).status_code == 403
        )
        note = client.post("/api/v1/notes", json={"title": "T", "body": "b"}, headers=auth(w))
        assert note.status_code == 201
        nid = note.json()["id"]
        assert client.delete(f"/api/v1/resources/{nid}", headers=auth(w)).status_code == 403
        assert client.post("/api/v1/reindex", headers=auth(w)).status_code == 403
        assert client.delete(f"/api/v1/resources/{nid}", headers=auth(a)).status_code == 204
        assert client.get(f"/api/v1/resources/{nid}", headers=auth(r)).status_code == 404

    def test_ssrf(self, client: TestClient, tokens: dict[str, str]) -> None:
        for url in ("http://192.168.1.1/", "http://localhost/"):
            res = client.post("/api/v1/resources", json={"url": url}, headers=auth(tokens["write"]))
            assert res.status_code == 400 and "SSRF" in res.json()["detail"]

    def test_upload_attachment(self, client: TestClient, tokens: dict[str, str]) -> None:
        w = tokens["write"]
        nid = client.post("/api/v1/notes", json={"title": "T"}, headers=auth(w)).json()["id"]
        res = client.post(
            f"/api/v1/resources/{nid}/attachments",
            files={"file": ("../report.txt", b"hello", "text/plain")},
            headers=auth(w),
        )
        assert res.status_code == 201 and res.json()["path"] == "report.txt"
        dl = client.get(f"/api/v1/resources/{nid}/files/attachments/report.txt", headers=auth(w))
        assert dl.content == b"hello"


def mcp_call(
    client: TestClient, token: str | None, method: str, params: dict[str, Any]
) -> httpx.Response:
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream",
               "mcp-protocol-version": "2025-06-18"}  # fmt: skip
    if token:
        headers |= auth(token)
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return client.post("/mcp", content=json.dumps(payload), headers=headers)


def tool(client: TestClient, token: str, name: str, **arguments: Any) -> dict[str, Any]:
    res = mcp_call(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert res.status_code == 200, res.text
    result: dict[str, Any] = res.json()["result"]
    return result


class TestMcp:
    def test_requires_token(self, client: TestClient) -> None:
        assert mcp_call(client, None, "tools/list", {}).status_code == 401

    def test_tools(self, client: TestClient, tokens: dict[str, str]) -> None:
        r, w = tokens["read"], tokens["write"]
        init = mcp_call(client, r, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}})  # fmt: skip
        assert "untrusted" in init.json()["result"]["instructions"]
        names = {t["name"] for t in mcp_call(client, r, "tools/list", {}).json()["result"]["tools"]}
        assert names == {
            "search",
            "get_resource",
            "get_content",
            "list_recent",
            "list_tags",
            "ingest_url",
            "create_note",
            "update_resource",
            "attach_url",
            "refetch",
        }
        assert not names & {"delete", "reindex"}

        denied = tool(client, r, "ingest_url", url=URL)
        assert denied["isError"] is True and "write scope" in denied["content"][0]["text"]

        made = tool(client, w, "ingest_url", url=URL, why="an essay", tags=["books"])
        rid = made["structuredContent"]["id"]
        wait_ready(client, rid, r)

        hits = tool(client, r, "search", query="papyrus")["structuredContent"]["results"]
        assert hits[0]["id"] == rid and "untrusted_snippet" in hits[0]
        detail = tool(client, r, "get_resource", ref=hits[0]["slug"])["structuredContent"]
        assert detail["why"] == "an essay" and detail["chunks"]
        page = tool(client, r, "get_content", ref=rid, chunk=0)["structuredContent"]
        assert page["source_url"] == URL and "untrusted_content" in page
        missing = tool(client, r, "get_resource", ref="nope")
        assert missing["isError"] is True and "search" in missing["content"][0]["text"]

        note = tool(client, w, "create_note", title="Idea", body="Use wombats")["structuredContent"]
        upd = tool(client, w, "update_resource", ref=note["slug"], why="for later")
        assert upd["structuredContent"]["status"] == "kept"
        assert tool(client, r, "list_tags")["structuredContent"]["tags"] == {"books": 1}


class TestUi:
    def login(self, client: TestClient, token: str) -> str:
        res = client.post("/login", data={"token": token, "next": "/"}, follow_redirects=False)
        assert res.status_code == 303
        page = client.get("/").text
        match = re.search(r'name="csrf-token" content="([0-9a-f]+)"', page)
        assert match
        return match.group(1)

    def test_login_required(self, client: TestClient) -> None:
        res = client.get("/inbox", follow_redirects=False)
        assert res.status_code == 303 and res.headers["location"].startswith("/login")
        bad = client.post("/login", data={"token": "nope"})
        assert bad.status_code == 401

    def test_pages_and_add_flow(self, client: TestClient, tokens: dict[str, str]) -> None:
        csrf = self.login(client, tokens["admin"])
        for path in ("/", "/inbox", "/add", "/activity", "/tags", "/settings", "/?q=x"):
            assert client.get(path).status_code == 200, path
        assert client.post("/add", data={"url": URL}).status_code == 403  # no CSRF token

        res = client.post("/add", data={"url": URL, "csrf": csrf}, follow_redirects=False)
        assert res.status_code == 303
        slug_page = res.headers["location"].split("?")[0]
        assert re.fullmatch(r"/r/[0-9a-f-]{36}", slug_page)  # by id: the slug may change
        assert client.get(slug_page).status_code == 200
        inbox = client.get("/inbox").text
        assert "Why keep this?" in inbox

        rid = client.get("/api/v1/search", headers=auth(tokens["read"])).json()["results"][0]["id"]
        wait_ready(client, rid, tokens["read"])
        res = client.post(f"/r/{rid}/why", data={"why": "Because", "csrf": csrf},
                          headers={"HX-Request": "true"})  # fmt: skip
        assert res.status_code == 200 and "Kept" in res.text
        assert "Inbox zero" in client.get("/inbox").text
        page = client.get(f"/r/{rid}")
        assert (
            "Papyrus" in page.text
            and "default-src 'self'" in page.headers["content-security-policy"]
        )
        assert client.get("/r/does-not-exist").status_code == 404

    def test_read_token_cannot_write(self, client: TestClient, tokens: dict[str, str]) -> None:
        csrf = self.login(client, tokens["read"])
        assert client.post("/add", data={"url": URL, "csrf": csrf}).status_code == 403

    def test_bootstrap_token(self, settings: Settings) -> None:
        from alexandria.app import ensure_bootstrap_token

        store = TokenStore(settings.tokens_path)
        secret = ensure_bootstrap_token(store)
        assert secret and store.verify(secret).scope == ScopeLevel.ADMIN  # type: ignore[union-attr]
        assert ensure_bootstrap_token(store) is None


class TestDocumentsAndImport:
    def test_rest_upload_and_import(self, client: TestClient, tokens: dict[str, str]) -> None:
        from tests.fixtures import make_docx

        w, r = tokens["write"], tokens["read"]
        res = client.post(
            "/api/v1/documents",
            files={"file": ("notes.docx", make_docx(), "application/octet-stream")},
            data={"why": "spec", "tags": "design, docs"},
            headers=auth(w),
        )
        assert res.status_code == 202, res.text
        data = wait_ready(client, res.json()["id"], r)
        assert data["kind"] == "document" and data["tags"] == ["design", "docs"]
        assert data["source_file"]["path"] == "source/notes.docx"
        assert data["meta"]["format"] == "docx"
        dl = client.get(f"/api/v1/resources/{data['id']}/files/source/notes.docx", headers=auth(r))
        assert dl.content == make_docx()
        hits = client.get(
            "/api/v1/search", params={"q": "fts5", "kind": "document"}, headers=auth(r)
        )
        assert [h["id"] for h in hits.json()["results"]] == [data["id"]]
        again = client.post("/api/v1/documents", files={"file": ("x.docx", make_docx())},
                            headers=auth(w))  # fmt: skip
        assert again.status_code == 200 and again.json()["duplicate"] is True
        assert client.post("/api/v1/documents", files={"file": ("y.txt", b"hi")},
                           headers=auth(r)).status_code == 403  # fmt: skip

        report = client.post(
            "/api/v1/import",
            json={"text": f"{URL}\nhttp://192.168.0.1/", "tags": ["batch"]},
            headers=auth(w),
        ).json()
        assert len(report["created"]) == 1 and len(report["errors"]) == 1

    def test_ui_upload(self, client: TestClient, tokens: dict[str, str]) -> None:
        csrf = TestUi().login(client, tokens["write"])
        res = client.post(
            "/upload",
            data={"csrf": csrf, "why": "", "tags": ""},
            files=[("files", ("a.md", b"# Alpha\n\nfirst")), ("files", ("b.txt", b"beta"))],
            follow_redirects=False,
        )
        assert res.status_code == 303 and res.headers["location"].startswith("/activity")
        assert client.get("/add?tab=file").status_code == 200
        assert client.get("/add?tab=import").status_code == 200
        res = client.post("/import", data={"csrf": csrf, "text": URL}, follow_redirects=False)
        assert res.status_code == 303
        assert "1 link(s) saved" in client.get(res.headers["location"]).text


class TestReviewRegressions:
    def test_anonymous_writes_rejected_before_body(self, client: TestClient) -> None:
        files = {"file": ("big.txt", b"x" * 100_000)}
        assert client.post("/api/v1/documents", files=files).status_code == 401
        assert client.post("/upload", files={"files": ("a.txt", b"x")}).status_code == 401
        assert client.post("/login", data={"token": "nope"}).status_code == 401  # still public

    def test_oversized_body(
        self, client: TestClient, settings: Settings, tokens: dict[str, str]
    ) -> None:
        too_big = str((settings.attachments.max_mb + 26) * 1_000_000)
        headers = auth(tokens["write"]) | {"content-length": too_big}
        res = client.post("/api/v1/documents", headers=headers, content=b"")
        assert res.status_code == 413

    def test_mcp_labels_extracted_meta(self, client: TestClient, tokens: dict[str, str]) -> None:
        made = tool(client, tokens["write"], "ingest_url", url=URL, why="x")
        rid = made["structuredContent"]["id"]
        wait_ready(client, rid, tokens["read"])
        detail = tool(client, tokens["read"], "get_resource", ref=rid)["structuredContent"]
        assert "meta" not in detail and detail["untrusted_meta"]["language"] == "en"
