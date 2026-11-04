"""ASGI application: REST API + web UI (FastAPI) and MCP (``/mcp``) in one process."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from alexandria import __version__
from alexandria.auth import Principal, ScopeLevel, TokenStore
from alexandria.config import Settings
from alexandria.context import AppContext
from alexandria.jobs import Worker
from alexandria.library import InvalidInput, Library, NotFound
from alexandria.mcp_server import McpAuth, build_mcp, mcp_asgi

log = logging.getLogger(__name__)

BOOTSTRAP_TOKEN_NAME = "admin"  # noqa: S105 - a token name, not a secret


def ensure_bootstrap_token(tokens: TokenStore) -> str | None:
    """First start with no tokens: create an admin token so the UI is reachable."""
    if not tokens.is_empty():
        return None
    return tokens.create(BOOTSTRAP_TOKEN_NAME, ScopeLevel.ADMIN)


def create_app(
    settings: Settings, library: Library | None = None, run_worker: bool = True
) -> ASGIApp:
    library = library or Library(settings)
    tokens = TokenStore(settings.tokens_path)
    ctx = AppContext(settings, library, Worker(library), tokens)
    mcp = build_mcp(ctx)
    mcp_app = mcp_asgi(mcp)  # must exist before session_manager is used

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        secret = ensure_bootstrap_token(tokens)
        if secret:
            banner = "=" * 72
            log.warning(
                "\n%s\nNo tokens existed, so an admin token was created. It is shown once:\n\n"
                "    %s\n\nUse it to sign in to the web UI. Manage tokens with "
                "`alexandria token ...`.\n%s",
                banner, secret, banner,
            )  # fmt: skip
        if run_worker:
            await ctx.worker.start()
        else:
            library.startup()
        async with mcp.session_manager.run():
            yield
        if run_worker:
            await ctx.worker.stop()

    api = FastAPI(title="Alexandria", version=__version__, lifespan=lifespan, docs_url="/api/docs")
    api.state.ctx = ctx

    @api.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @api.exception_handler(InvalidInput)
    async def _invalid(_: Request, exc: InvalidInput) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @api.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path.startswith("/api/") or exc.status_code < 400 or exc.headers:
            return await http_exception_handler(request, exc)
        from alexandria.ui.routes import render

        return render(
            request,
            "error.html",
            code=exc.status_code,
            status_code=exc.status_code,
            message=exc.detail,
        )

    @api.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    from alexandria.api.routes import router as api_router
    from alexandria.ui.routes import router as ui_router

    api.include_router(api_router)
    api.include_router(ui_router)
    static = Path(__file__).parent / "ui" / "static"
    api.mount("/static", StaticFiles(directory=static), name="static")

    max_body = (settings.attachments.max_mb + 25) * 1_000_000  # uploads plus form overhead
    # Library import parts are much larger than attachments; only that route takes them.
    large = {"/settings/import": (settings.export.max_upload_mb + 25) * 1_000_000}
    guarded = RequestGuard(api, tokens, settings.http.session_secret or "", max_body, large)
    return Root(guarded, BodyCap(McpAuth(mcp_app, tokens), MCP_MAX_BODY))


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_PUBLIC_POSTS = {"/login"}
# MCP messages are JSON-RPC calls: notes and tool arguments, never files. The MCP SDK
# reads a request body whole into memory, so it is capped well below uploads.
MCP_MAX_BODY = 10_000_000


class RequestTooLarge(Exception):
    pass


async def _capped(app: ASGIApp, scope: Scope, receive: Receive, send: Send, max_body: int) -> None:
    """Run ``app``, answering 413 when the body grows past ``max_body`` (chunked bodies
    have no Content-Length to check up front)."""
    received = 0
    started = overflow = False

    async def capped_receive() -> Any:
        nonlocal received, overflow
        message = await receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > max_body:
                overflow = True
                raise RequestTooLarge
        return message

    async def tracked_send(message: Any) -> None:
        nonlocal started
        if overflow:
            return  # Starlette's error middleware answers 500 before re-raising: drop it
        started = started or message["type"] == "http.response.start"
        await send(message)

    try:
        await app(scope, capped_receive, tracked_send)
    except RequestTooLarge:
        if started:
            raise  # too late for a status code: the server drops the connection
        await _plain(send, 413, "request body too large")


def _too_long(scope: Scope, max_body: int) -> bool:
    for key, value in scope["headers"]:
        if key.lower() == b"content-length":
            return value.isdigit() and int(value) > max_body
    return False


class BodyCap:
    """Cap the body of write requests (used for ``/mcp``, which RequestGuard does not see)."""

    def __init__(self, app: ASGIApp, max_body: int) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _WRITE_METHODS:
            await self.app(scope, receive, send)
        elif _too_long(scope, self.max_body):
            await _plain(send, 413, "request body too large")
        else:
            await _capped(self.app, scope, receive, send, self.max_body)


class RequestGuard:
    """Authenticate writes and cap their size *before* any body is read.

    FastAPI parses form bodies (uploads) before it runs auth dependencies, so without this
    an anonymous client, or a read-only token, could make the server spool arbitrary
    uploads to disk. Routes still check scopes and CSRF themselves; this only rejects
    requests whose credential cannot possibly be enough: none at all, a read token on an
    API write, or less than admin on the large upload routes.
    """

    def __init__(
        self,
        app: ASGIApp,
        tokens: TokenStore,
        secret: str,
        max_body: int,
        large_paths: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.tokens = tokens
        self.secret = secret
        self.max_body = max_body
        self.large_paths = large_paths or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _WRITE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope["headers"]}
        path: str = scope["path"]
        max_body = self.large_paths.get(path, self.max_body)
        if _too_long(scope, max_body):
            await _plain(send, 413, "request body too large")
            return
        if path not in _PUBLIC_POSTS:
            who = self._principal(path, headers)
            if who is None:
                await _plain(send, 401, "authentication required")
                return
            needed = (
                ScopeLevel.ADMIN if path in self.large_paths
                else ScopeLevel.WRITE if path.startswith("/api/")
                else ScopeLevel.READ
            )  # fmt: skip
            if not who.can(needed):
                await _plain(send, 403, f"this needs a {needed.name.lower()} token")
                return
        await _capped(self.app, scope, receive, send, max_body)

    def _principal(self, path: str, headers: dict[str, str]) -> Principal | None:
        from alexandria.auth import bearer
        from alexandria.ui.routes import COOKIE, session_principal

        if path.startswith("/api/"):
            return self.tokens.verify(bearer(headers.get("authorization")))
        cookies = SimpleCookie()
        cookies.load(headers.get("cookie", ""))
        cookie = cookies[COOKIE].value if COOKIE in cookies else None
        return session_principal(cookie, self.secret, self.tokens)


async def _plain(send: Send, status: int, message: str) -> None:
    body = json.dumps({"detail": message}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})  # fmt: skip
    await send({"type": "http.response.body", "body": body})


class Root:
    """Send ``/mcp`` to the MCP transport and everything else (lifespan included) to FastAPI."""

    def __init__(self, api: ASGIApp, mcp: ASGIApp) -> None:
        self.api = api
        self.mcp = mcp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            scope = dict(scope, path="/mcp")
            await self.mcp(scope, receive, send)
            return
        await self.api(scope, receive, send)
