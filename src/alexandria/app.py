"""ASGI application: REST API + web UI (FastAPI) and MCP (``/mcp``) in one process."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from alexandria import __version__
from alexandria.auth import ScopeLevel, TokenStore
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

    return Root(api, McpAuth(mcp_app, tokens))


class Root:
    """Send ``/mcp`` to the MCP transport and everything else (lifespan included) to FastAPI."""

    def __init__(self, api: FastAPI, mcp: ASGIApp) -> None:
        self.api = api
        self.mcp = mcp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            scope = dict(scope, path="/mcp")
            await self.mcp(scope, receive, send)
            return
        await self.api(scope, receive, send)
