"""REST API, MCP and web UI, through the real ASGI app (lifespan, worker and all)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote_plus

import httpx
import pytest
from starlette.testclient import TestClient

from alexandria.app import MCP_MAX_BODY
from alexandria.auth import ScopeLevel, TokenStore
from alexandria.config import Settings
from tests.conftest import URL


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


def wait_job(client: TestClient, job_id: str, token: str, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data: dict[str, Any] = client.get(f"/api/v1/jobs/{job_id}", headers=auth(token)).json()
        if data["status"] in ("done", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError("job did not finish")


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
            "list_projects",
            "lookup_url",
            "get_job",
            "list_jobs",
        }
        assert not names & {"delete", "reindex"}
        unsaved = tool(client, r, "lookup_url", url=URL)["structuredContent"]
        assert unsaved == {"saved": False}

        denied = tool(client, r, "ingest_url", url=URL)
        assert denied["isError"] is True and "write scope" in denied["content"][0]["text"]

        made = tool(client, w, "ingest_url", url=URL, why="an essay", tags=["books"])
        rid = made["structuredContent"]["id"]
        wait_ready(client, rid, r)
        job = tool(client, r, "get_job", job_id=made["structuredContent"]["job_id"])
        assert job["structuredContent"]["status"] == "done"
        assert tool(client, r, "get_job", job_id="nope")["isError"] is True
        listed = tool(client, r, "list_jobs", status="done")["structuredContent"]["jobs"]
        assert [j["resource_id"] for j in listed] == [rid]
        saved = tool(client, r, "lookup_url", url=URL + "?utm_source=x#top")["structuredContent"]
        assert saved["saved"] is True and saved["id"] == rid and saved["why"] == "an essay"

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
        tool(client, w, "create_note", title="Plan", body="x", project="garden")
        projects = tool(client, r, "list_projects")["structuredContent"]["projects"]
        assert projects == {"garden": 1}
        rest = client.get("/api/v1/lookup", params={"url": URL}, headers=auth(r)).json()
        assert rest["saved"] is True and rest["id"] == rid
        assert client.get("/api/v1/projects", headers=auth(r)).json() == {"projects": {"garden": 1}}


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

    def test_htmx_failures_are_plain_messages(
        self, client: TestClient, tokens: dict[str, str]
    ) -> None:
        csrf = self.login(client, tokens["write"])
        made = client.post("/api/v1/notes", json={"title": "Alpha", "body": "x"},
                           headers=auth(tokens["write"])).json()  # fmt: skip
        res = client.post(f"/r/{made['id']}/status/kept", data={"csrf": csrf},
                          headers={"HX-Request": "true"})  # fmt: skip
        assert res.status_code == 400 and res.headers["content-type"].startswith("text/plain")
        assert "needs a 'why'" in res.text and "<html" not in res.text
        js = client.get("/static/app.js").text
        assert "htmx:responseError" in js and "htmx:sendError" in js

    def test_read_token_sees_no_write_controls(
        self, client: TestClient, tokens: dict[str, str]
    ) -> None:
        made = client.post("/api/v1/notes", json={"title": "Alpha", "body": "x"},
                           headers=auth(tokens["write"])).json()  # fmt: skip
        self.login(client, tokens["read"])
        page = client.get(f"/r/{made['id']}").text
        assert "No reason given yet." in page and "Save details" not in page
        assert "Save reason" not in page and "Archive</button>" not in page
        inbox = client.get("/inbox").text
        assert "Alpha" in inbox and "Keep</button>" not in inbox and "Archive" not in inbox

    def test_archive_from_the_inbox_stays_there(
        self, client: TestClient, tokens: dict[str, str]
    ) -> None:
        csrf = self.login(client, tokens["admin"])
        a, b = (
            client.post("/api/v1/notes", json={"title": t, "body": "x"},
                        headers=auth(tokens["write"])).json()
            for t in ("Alpha", "Beta")
        )  # fmt: skip
        assert "Alpha" in client.get("/inbox").text
        res = client.post(f"/r/{a['id']}/status/archived", data={"csrf": csrf, "back": "inbox"},
                          headers={"HX-Request": "true"})  # fmt: skip
        assert res.status_code == 200 and 'id="inbox-' in res.text and "archived" in res.text
        res = client.post(f"/r/{b['id']}/status/archived", data={"csrf": csrf, "back": "inbox"},
                          follow_redirects=False)  # fmt: skip
        assert res.headers["location"].startswith("/inbox?notice=")
        assert "Alpha" not in client.get("/inbox").text

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
        assert '<a href="/inbox" class="on" aria-current="page">' in inbox
        assert 'aria-label="Sign out"' in inbox
        again = client.post("/add", data={"url": URL, "csrf": csrf, "why": "for the talk"})
        assert "Already in the library: your reason added." in again.text
        client.post(f"/r/{again.url.path.split('/')[-1]}/why", data={"why": "", "csrf": csrf})

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

    def test_attach_several_files_and_a_link(
        self, client: TestClient, tokens: dict[str, str]
    ) -> None:
        csrf = self.login(client, tokens["write"])
        w = auth(tokens["write"])
        nid = client.post("/api/v1/notes", json={"title": "T"}, headers=w).json()["id"]
        page = client.get(f"/r/{nid}").text
        assert 'data-dialog="#attach-link"' in page and "multiple" in page
        res = client.post(
            f"/r/{nid}/attachments",
            data={"csrf": csrf},
            files=[("file", ("a.txt", b"alpha")), ("file", ("b.txt", b"beta"))],
            follow_redirects=False,
        )
        assert res.status_code == 303 and "Attached+2+files" in res.headers["location"]
        paths = [
            a["path"]
            for a in client.get(f"/api/v1/resources/{nid}", headers=w).json()["attachments"]
        ]
        assert paths == ["attachments/a.txt", "attachments/b.txt"]
        res = client.post(f"/r/{nid}/attachments", data={"csrf": csrf, "url": "http://10.0.0.1/x"},
                          follow_redirects=False)  # fmt: skip
        assert "error=" in res.headers["location"] and "SSRF" in unquote_plus(
            res.headers["location"]
        )

    def test_delete_attachment_and_edit_in_place(
        self, client: TestClient, tokens: dict[str, str]
    ) -> None:
        w = auth(tokens["write"])
        nid = client.post("/api/v1/notes", json={"title": "T", "body": "hello"}, headers=w).json()[
            "id"
        ]
        for name in ("keep.txt", "drop.txt"):
            client.post(
                f"/api/v1/resources/{nid}/attachments", files={"file": (name, b"x")}, headers=w
            )
        assert (
            client.delete(f"/api/v1/resources/{nid}/attachments/drop.txt", headers=w).status_code
            == 403
        )

        csrf = self.login(client, tokens["write"])
        page = client.get(f"/r/{nid}").text
        assert "/attachments/drop.txt/delete" not in page  # admin only
        assert 'href="/r/' in page and "?edit=note" in page
        editing = client.get(f"/r/{nid}?edit=note").text
        assert '<textarea name="notes" rows="18" autofocus>hello</textarea>' in editing

        csrf = self.login(client, tokens["admin"])
        assert "/attachments/drop.txt/delete" in client.get(f"/r/{nid}").text
        res = client.post(f"/r/{nid}/attachments/drop.txt/delete", data={"csrf": csrf},
                          follow_redirects=False)  # fmt: skip
        assert res.status_code == 303 and "Deleted+drop.txt" in res.headers["location"]
        a = auth(tokens["admin"])
        assert (
            client.delete(f"/api/v1/resources/{nid}/attachments/keep.txt", headers=a).status_code
            == 204
        )
        assert (
            client.delete(f"/api/v1/resources/{nid}/attachments/keep.txt", headers=a).status_code
            == 404
        )
        assert client.get(f"/api/v1/resources/{nid}", headers=a).json().get("attachments", []) == []
        assert (
            client.get(f"/api/v1/resources/{nid}/files/attachments/keep.txt", headers=a).status_code
            == 404
        )

    def test_delete_snapshot_ui_and_api(self, client: TestClient, tokens: dict[str, str]) -> None:
        w, a = auth(tokens["write"]), auth(tokens["admin"])
        rid = client.post("/api/v1/documents", files={"file": ("n.md", b"# Hi\n\nheron")},
                          headers=w).json()["id"]  # fmt: skip
        wait_ready(client, rid, tokens["read"])
        client.post(f"/api/v1/resources/{rid}/refetch", headers=w)
        deadline = time.monotonic() + 10
        while len(client.get(f"/api/v1/resources/{rid}", headers=a).json()["snapshots"]) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        first = client.get(f"/api/v1/resources/{rid}", headers=a).json()["snapshots"][0]["id"]
        url = f"/api/v1/resources/{rid}/snapshots/{first}"
        assert client.delete(url, headers=w).status_code == 403

        csrf = self.login(client, tokens["admin"])
        assert f"/snapshots/{first}/delete" in client.get(f"/r/{rid}").text
        res = client.post(f"/r/{rid}/snapshots/{first}/delete", data={"csrf": csrf},
                          follow_redirects=False)  # fmt: skip
        assert res.status_code == 303 and "Deleted+the+snapshot" in res.headers["location"]
        assert client.delete(url, headers=a).status_code == 404
        only = client.get(f"/api/v1/resources/{rid}", headers=a).json()["snapshots"][0]["id"]
        last = client.delete(f"/api/v1/resources/{rid}/snapshots/{only}", headers=a)
        assert last.status_code == 400 and "only snapshot" in last.json()["detail"]
        assert f"/snapshots/{only}/delete" not in client.get(f"/r/{rid}").text

    def test_manage_tokens(self, client: TestClient, tokens: dict[str, str]) -> None:
        csrf = self.login(client, tokens["write"])
        assert client.post("/settings/tokens", data={"csrf": csrf, "name": "x"}).status_code == 403
        assert "New token" not in client.get("/settings").text

        csrf = self.login(client, tokens["admin"])
        res = client.post(
            "/settings/tokens", data={"csrf": csrf, "name": "laptop", "scope": "write"}
        )
        assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
        match = re.search(r'<pre id="new-token">(alx_[^<]+)</pre>', res.text)
        assert match, "the secret is shown once"
        secret = match.group(1)
        assert client.get("/api/v1/tags", headers=auth(secret)).status_code == 200
        assert secret not in client.get("/settings").text  # never again

        dup = client.post("/settings/tokens", data={"csrf": csrf, "name": "laptop"})
        assert "already exists" in dup.text
        bad = client.post("/settings/tokens", data={"csrf": csrf, "name": "<script>"})
        assert "up to 40 letters" in bad.text

        res = client.post("/settings/tokens/laptop/scope", data={"csrf": csrf, "scope": "read"},
                          follow_redirects=False)  # fmt: skip
        assert "laptop+is+now+read" in res.headers["location"]
        body = {"url": "https://a.example/x"}
        assert client.post("/api/v1/resources", json=body, headers=auth(secret)).status_code == 403

        res = client.post("/settings/tokens/laptop/revoke", data={"csrf": csrf},
                          follow_redirects=False)  # fmt: skip
        assert "Revoked+laptop" in res.headers["location"]
        assert client.get("/api/v1/tags", headers=auth(secret)).status_code == 401

        # The signed-in token and the last admin token are protected.
        me = client.post(
            "/settings/tokens/admin/revoke", data={"csrf": csrf}, follow_redirects=False
        )
        assert "error=" in me.headers["location"]

    def test_last_admin_is_kept(self, settings: Settings) -> None:
        from alexandria.auth import TokenError

        store = TokenStore(settings.tokens_path)
        store.create("root", ScopeLevel.ADMIN)
        store.create("bot", ScopeLevel.WRITE)
        with pytest.raises(TokenError, match="last admin"):
            store.revoke("root", keep_an_admin=True)
        with pytest.raises(TokenError, match="last admin"):
            store.set_scope("root", ScopeLevel.READ)
        store.set_scope("bot", ScopeLevel.ADMIN)
        assert store.revoke("root", keep_an_admin=True)
        store.set_scope("bot", ScopeLevel.READ, keep_an_admin=False)  # the CLI may (recovery)

    def test_cookie_is_secure_behind_an_https_proxy(
        self, client: TestClient, tokens: dict[str, str], settings: Settings
    ) -> None:
        res = client.post("/login", data={"token": tokens["read"]}, follow_redirects=False)
        assert "secure" not in res.headers["set-cookie"].lower()  # plain http on the LAN
        settings.http.public_url = "https://library.example.org"
        res = client.post("/login", data={"token": tokens["read"]}, follow_redirects=False)
        cookie = res.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie

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

        queued = client.post(
            "/api/v1/import",
            json={"text": f"{URL}\nhttp://192.168.0.1/", "tags": ["batch"]},
            headers=auth(w),
        )
        assert queued.status_code == 202 and queued.json()["links"] == 2
        job = wait_job(client, queued.json()["job_id"], r)
        report = job["payload"]["report"]
        assert job["status"] == "done" and "entries" not in job["payload"]
        assert report["created"] == 1 and report["error_count"] == 1
        assert "192.168.0.1" in report["errors"][0]
        assert client.get("/api/v1/jobs/nope", headers=auth(r)).status_code == 404

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
        assert "Importing 1 link(s) in the background." in client.get(res.headers["location"]).text
        nothing = client.post("/import", data={"csrf": csrf, "text": "no links here"})
        assert "no links found" in nothing.text


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

    def test_mcp_body_is_capped(self, client: TestClient, tokens: dict[str, str]) -> None:
        headers = auth(tokens["read"]) | {"content-type": "application/json"}
        big = {"content-length": str(MCP_MAX_BODY + 1)}
        assert client.post("/mcp", headers=headers | big, content=b"").status_code == 413

        def body() -> Iterator[bytes]:
            for _ in range(MCP_MAX_BODY // 1_000_000 + 1):
                yield b" " * 1_000_000

        # Chunked: no Content-Length, so the cap applies while the body is read.
        assert client.post("/mcp", headers=headers, content=body()).status_code == 413

    def test_read_token_cannot_start_an_upload(
        self, client: TestClient, tokens: dict[str, str]
    ) -> None:
        files = {"file": ("big.txt", b"x" * 100_000)}
        res = client.post("/api/v1/documents", files=files, headers=auth(tokens["read"]))
        assert res.status_code == 403 and "write token" in res.text

    def test_mcp_labels_extracted_meta(self, client: TestClient, tokens: dict[str, str]) -> None:
        made = tool(client, tokens["write"], "ingest_url", url=URL, why="x")
        rid = made["structuredContent"]["id"]
        wait_ready(client, rid, tokens["read"])
        detail = tool(client, tokens["read"], "get_resource", ref=rid)["structuredContent"]
        assert "meta" not in detail and detail["untrusted_meta"]["language"] == "en"
