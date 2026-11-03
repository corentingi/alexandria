"""Web UI (design §10.3): Jinja2 + vendored HTMX, no build step.

Sign-in: paste a token once; it is kept in a signed, HttpOnly, SameSite=Lax cookie and
re-verified on every request, so revoking a token signs its sessions out. Every POST
carries a CSRF token derived from the session.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from markdown_it import MarkdownIt
from markupsafe import Markup, escape
from starlette.datastructures import UploadFile as StarletteUpload

from alexandria import __version__
from alexandria.auth import Principal, ScopeLevel, TokenStore
from alexandria.context import AppContext
from alexandria.extract import detect_format
from alexandria.library import InvalidInput, NotFound, Update
from alexandria.models import Kind, Resource, Status, iso
from alexandria.storage.index import HL_END, HL_START, SearchFilters
from alexandria.views import plain_snippet
from alexandria.walls import site_warning

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
COOKIE = "alx_session"
PAGE_SIZE = 25

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(
    "table"
)
_md.add_render_rule(
    "link_open",
    lambda self, tokens, idx, options, env: (
        tokens[idx].attrSet("rel", "noopener noreferrer nofollow")
        or tokens[idx].attrSet("target", "_blank")
        or self.renderToken(tokens, idx, options, env)
    ),
)
# Remote images would leak reads to third parties and bypass "content is data": show links.
_md.add_render_rule(
    "image",
    lambda self, tokens, idx, options, env: str(
        Markup('<a class="img-link" href="{0}" rel="noopener noreferrer nofollow" '
               'target="_blank">[image: {1}]</a>').format(
            tokens[idx].attrGet("src") or "", tokens[idx].content or "image"
        )
    ),
)  # fmt: skip


# --------------------------------------------------------------------- helpers


def ctx(request: Request) -> AppContext:
    context: AppContext = request.app.state.ctx
    return context


def _serializer(request: Request) -> URLSafeSerializer:
    return URLSafeSerializer(ctx(request).settings.http.session_secret or "", salt="session")


def _csrf_for(request: Request, cookie: str) -> str:
    secret = (ctx(request).settings.http.session_secret or "").encode()
    return hmac.new(secret, cookie.encode(), hashlib.sha256).hexdigest()[:32]


def session_principal(cookie: str | None, secret: str, tokens: TokenStore) -> Principal | None:
    """The principal behind a session cookie (its token is re-verified every time)."""
    if not cookie:
        return None
    try:
        data = URLSafeSerializer(secret, salt="session").loads(cookie)
    except BadSignature:
        return None
    return tokens.verify(data.get("t") if isinstance(data, dict) else None)


def current_user(request: Request) -> Principal | None:
    c = ctx(request)
    return session_principal(
        request.cookies.get(COOKIE), c.settings.http.session_secret or "", c.tokens
    )


def _login_redirect(request: Request) -> HTTPException:
    target = "/login?" + urlencode(
        {"next": request.url.path + (f"?{request.url.query}" if request.url.query else "")}
    )
    if request.headers.get("hx-request"):
        return HTTPException(401, headers={"HX-Redirect": target})
    return HTTPException(303, headers={"Location": target})


def user(request: Request) -> Principal:
    found = current_user(request)
    if found is None:
        raise _login_redirect(request)
    return found


async def csrf(request: Request) -> None:
    if request.method != "POST":
        return
    cookie = request.cookies.get(COOKIE, "")
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        form = await request.form()
        value = form.get("csrf")
        sent = value if isinstance(value, str) else None
    if not cookie or not sent or not hmac.compare_digest(sent, _csrf_for(request, cookie)):
        raise HTTPException(403, "invalid or missing CSRF token; reload the page")


User = Annotated[Principal, Depends(user)]
Ctx = Annotated[AppContext, Depends(ctx)]
Csrf = Depends(csrf)


def need(who: Principal, level: ScopeLevel) -> None:
    if not who.can(level):
        raise HTTPException(403, f"your token does not have the {level.label} scope")


def render(request: Request, name: str, *, code: int = 200, **data: Any) -> HTMLResponse:
    who = current_user(request)
    cookie = request.cookies.get(COOKIE, "")
    c = ctx(request)
    base = {
        "request": request,
        "user": who,
        "csrf": _csrf_for(request, cookie) if cookie else "",
        "version": __version__,
        "inbox_count": c.library.index.counts().get("inbox", 0) if who else 0,
        "active_jobs": len(c.library.index.jobs("running")) + len(c.library.index.jobs("queued"))
        if who
        else 0,
        "path": request.url.path,
    }
    response = templates.TemplateResponse(request, name, base | data, status_code=code)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def back(url: str, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in {"notice": notice, "error": error}.items() if v}
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (sep + urlencode(params) if params else ""), status_code=303)


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


# -------------------------------------------------------------- template filters


def _host(url: str | None) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.") if url else ""


def _ago(value: str | datetime | None) -> str:
    if not value:
        return ""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    seconds = (datetime.now(UTC) - dt).total_seconds()
    steps = [(60, "s", 1), (3600, "min", 60), (86400, "h", 3600), (86400 * 30, "d", 86400),
             (86400 * 365, "mo", 86400 * 30)]  # fmt: skip
    for limit, unit, size in steps:
        if seconds < limit:
            return f"{max(0, int(seconds // size))} {unit} ago"
    return f"{int(seconds // (86400 * 365))} y ago"


def _size(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1024:
        return f"{n} B"
    exp = min(int(math.log(n, 1024)), 4)
    return f"{n / 1024**exp:.1f} {'BKMGT'[exp]}B".replace(" BB", " B")


def _highlight(snippet: str | None) -> Markup:
    if not snippet:
        return Markup("")
    text = str(escape(plain_snippet(snippet)))
    # The snippet was escaped above; only our own markers become tags.
    return Markup(text.replace(HL_START, "<mark>").replace(HL_END, "</mark>"))  # noqa: S704


def _markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))  # noqa: S704 - raw HTML is disabled in the parser


def _date(value: str | datetime | None) -> str:
    if not value:
        return ""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return dt.strftime("%Y-%m-%d %H:%M")


def _snapshot_date(snapshot_id: str) -> str:
    try:
        return datetime.strptime(snapshot_id, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return snapshot_id


templates.env.filters.update(
    host=_host, ago=_ago, filesize=_size, highlight=_highlight, markdown=_markdown,
    date=_date, snapdate=_snapshot_date, urlquote=lambda s: quote(s, safe=""),
)  # fmt: skip
templates.env.globals.update(Kind=Kind, Status=Status, ScopeLevel=ScopeLevel)


# ---------------------------------------------------------------------- session


@router.get("/login")
async def login_page(request: Request, next: str = "/", error: str | None = None) -> Response:
    return render(request, "login.html", next=_safe_next(next), error=error)


@router.post("/login")
async def login(
    request: Request, token: Annotated[str, Form()], next: Annotated[str, Form()] = "/"
) -> Response:
    principal = ctx(request).tokens.verify(token.strip())
    if principal is None:
        return render(
            request, "login.html", code=401, next=_safe_next(next), error="Unknown token."
        )
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        COOKIE,
        _serializer(request).dumps({"t": token.strip()}),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 90,
        secure=request.url.scheme == "https",
    )
    return response


@router.post("/logout", dependencies=[Csrf])
async def logout(_: User) -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


# ----------------------------------------------------------------------- search


def _filters(
    kind: str | None, status: str | None, tag: list[str], project: str | None
) -> SearchFilters:
    return SearchFilters(
        kind=kind or None,
        status=status or None,
        tags=[t for t in tag if t],
        project=project or None,
    )


@router.get("/")
async def home(
    request: Request,
    c: Ctx,
    who: User,
    q: str = "",
    kind: str | None = None,
    status: str | None = None,
    project: str | None = None,
    page: int = 1,
) -> Response:
    tags = request.query_params.getlist("tag")
    filters = _filters(kind, status, tags, project)
    page = max(1, page)
    hits = c.library.search(q, filters, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)
    data: dict[str, Any] = {
        "q": q,
        "kind": kind or "",
        "status": status or "",
        "tags": filters.tags,
        "project": project or "",
        "hits": hits[:PAGE_SIZE],
        "page": page,
        "has_more": len(hits) > PAGE_SIZE,
        "all_tags": c.library.tags()[:40],
        "prev_url": _page_url(request, page - 1) if page > 1 else None,
        "next_url": _page_url(request, page + 1),
    }
    if request.headers.get("hx-request") and request.headers.get("hx-target") == "results":
        return render(request, "_results.html", **data)
    data["stats"] = _stats(c)
    return render(request, "search.html", **data)


def _page_url(request: Request, page: int) -> str:
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "page"]
    return "/?" + urlencode([*params, ("page", str(page))])


def _stats(c: AppContext) -> dict[str, Any]:
    with c.library.index.read() as db:
        rows = db.execute(
            "SELECT kind, count(*) AS n FROM resources WHERE status != 'archived' GROUP BY kind"
        )
        kinds = {r["kind"]: r["n"] for r in rows}
        week = db.execute(
            "SELECT count(*) AS n FROM resources WHERE captured_at >= date('now', '-7 days')"
        ).fetchone()["n"]
        size = db.execute("SELECT coalesce(sum(size), 0) AS n FROM artifacts").fetchone()["n"]
        failed = db.execute(
            "SELECT count(*) AS n FROM resources WHERE fetch_status = 'failed'"
        ).fetchone()["n"]
    return {"kinds": kinds, "week": week, "bytes": size, "failed": failed,
            "total": sum(kinds.values())}  # fmt: skip


@router.get("/tags")
async def tags_page(request: Request, c: Ctx, who: User) -> Response:
    tags = c.library.tags()
    top = max((n for _, n in tags), default=1)
    return render(request, "tags.html", tags=tags, top=top)


# ------------------------------------------------------------------------ inbox


@router.get("/inbox")
async def inbox(request: Request, c: Ctx, who: User) -> Response:
    hits = c.library.search("", SearchFilters(status="inbox"), 200)
    return render(request, "inbox.html", hits=hits)


@router.post("/r/{ref}/why", dependencies=[Csrf])
async def set_why(
    request: Request, ref: str, c: Ctx, who: User, why: Annotated[str, Form()] = ""
) -> Response:
    need(who, ScopeLevel.WRITE)
    try:
        resource = await c.library.update(ref, Update(why=why))
    except InvalidInput as exc:
        return back(f"/r/{ref}", error=str(exc))
    if request.headers.get("hx-request"):
        return render(request, "_inbox_done.html", r=resource)
    return back(f"/r/{resource.slug}", notice="Reason saved.")


# -------------------------------------------------------------------------- add


@router.get("/add")
async def add_page(
    request: Request, who: User, url: str = "", title: str = "", tab: str = "url"
) -> Response:
    warning = site_warning(url) if url else None
    return render(request, "add.html", url=url, title=title, tab=tab, warning=warning)


@router.get("/add/check")
async def add_check(request: Request, who: User, url: str = "") -> Response:
    """Live warning under the URL field for sites that need a login or block fetches."""
    return render(request, "_site_warning.html", warning=site_warning(url) if url else None)


@router.post("/add", dependencies=[Csrf])
async def add(
    request: Request,
    c: Ctx,
    who: User,
    url: Annotated[str, Form()],
    why: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    project: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
) -> Response:
    need(who, ScopeLevel.WRITE)
    try:
        result = await c.library.ingest(
            url,
            why=why or None,
            tags=_split_tags(tags),
            project=project or None,
            kind=Kind(kind) if kind else None,
            title=title or None,
            created_by=who.name,
        )
    except InvalidInput as exc:
        return render(request, "add.html", code=400, url=url, title=title, why=why, tags=tags,
                      project=project, tab="url", error=str(exc))  # fmt: skip
    c.enqueued()
    notice = "Already in the library." if result.duplicate else "Saved. Fetching in the background…"
    # By id: an article's slug may still change once its title is extracted (ADR-0008).
    return back(f"/r/{result.resource.id}", notice=notice)


@router.post("/notes/new", dependencies=[Csrf])
async def add_note(
    request: Request,
    c: Ctx,
    who: User,
    title: Annotated[str, Form()],
    body: Annotated[str, Form()] = "",
    why: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    project: Annotated[str, Form()] = "",
) -> Response:
    need(who, ScopeLevel.WRITE)
    try:
        note = await c.library.create_note(
            title, body, why or None, _split_tags(tags), None, project or None, who.name
        )
    except InvalidInput as exc:
        return render(request, "add.html", code=400, tab="note", note_title=title, body=body,
                      why=why, tags=tags, project=project, error=str(exc))  # fmt: skip
    return back(f"/r/{note.slug}", notice="Note saved.")


@router.post("/upload", dependencies=[Csrf])
async def upload(
    request: Request,
    c: Ctx,
    who: User,
    why: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    project: Annotated[str, Form()] = "",
) -> Response:
    need(who, ScopeLevel.WRITE)
    form = await request.form()
    files = [f for f in form.getlist("files") if isinstance(f, StarletteUpload) and f.filename]
    if not files:
        return back("/add?tab=file", error="Choose at least one file.")
    saved, duplicates, errors = [], 0, []
    for file in files:
        try:
            result = await c.library.ingest_file(
                file.filename or "document", _chunks(file), file.content_type,
                why=why or None, tags=_split_tags(tags), project=project or None,
                created_by=who.name,
            )  # fmt: skip
        except InvalidInput as exc:
            errors.append(f"{file.filename}: {exc}")
            continue
        duplicates += result.duplicate
        saved.append(result.resource)
    c.enqueued()
    if len(saved) == 1 and not errors:
        note = "Already in the library." if duplicates else "Saved. Extracting text…"
        return back(f"/r/{saved[0].id}", notice=note)
    notice = f"{len(saved) - duplicates} document(s) saved, {duplicates} already there."
    return back("/activity", notice=notice, error="; ".join(errors) or None)


@router.post("/import", dependencies=[Csrf])
async def import_links(
    request: Request,
    c: Ctx,
    who: User,
    text: Annotated[str, Form()] = "",
    why: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    project: Annotated[str, Form()] = "",
) -> Response:
    need(who, ScopeLevel.WRITE)
    form = await request.form()
    upload = form.get("bookmarks")
    if isinstance(upload, StarletteUpload) and upload.filename:
        text += "\n" + (await upload.read(20_000_000)).decode("utf-8", errors="replace")
    if not text.strip():
        return back("/add?tab=import", error="Paste some links or choose a bookmarks file.")
    report = await c.library.import_urls(
        text, why or None, _split_tags(tags), project or None, who.name
    )
    c.enqueued()
    notice = (f"{len(report.created)} link(s) saved, {len(report.duplicates)} already there, "
              f"{len(report.errors)} rejected.")  # fmt: skip
    return back("/activity", notice=notice, error="; ".join(report.errors[:5]) or None)


def _split_tags(raw: str) -> list[str]:
    return [t for t in raw.replace(",", " ").split() if t]


# --------------------------------------------------------------------- resource


def _load(c: AppContext, ref: str) -> Resource:
    try:
        return c.library.get(ref)
    except NotFound as exc:
        raise HTTPException(404, "No such resource.") from exc


@router.get("/r/{ref}")
async def resource_page(
    request: Request,
    c: Ctx,
    who: User,
    ref: str,
    snapshot: str | None = None,
    edit: str | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> Response:
    resource = _load(c, ref)
    shown = resource.snapshot(snapshot) if snapshot else resource.snapshot()
    from_attachment = None
    content, role = "", None
    if resource.kind == Kind.NOTE:
        content, role = resource.notes, "note"
    elif shown is not None:
        for wanted in ("content", "readme"):
            artifact = next((a for a in shown.artifacts if a.role == wanted), None)
            if artifact:
                path = c.library.files.snapshot_dir(resource.id, shown.id) / artifact.path
                exists = path.exists()
                content = path.read_text(encoding="utf-8", errors="replace") if exists else ""
                role = wanted
                break
    if snapshot is None and resource.kind != Kind.NOTE:
        text, used = c.library.text(resource)
        if used == "attachment":  # the page is blocked or empty: show the attachment's text
            found = c.library.attachment_text(resource)
            content, role = text, "attachment"
            from_attachment = found[1].path if found else None
    directory = c.library.files.attachments_dir(resource.id)
    extractable = {
        a.path
        for a in resource.attachments
        if a.extraction is None
        and (directory / a.path).is_file()
        and detect_format(directory / a.path, a.path, a.mime)
    }
    url = resource.canonical_url or resource.source_url
    warning = site_warning(url) if url and resource.kind == Kind.ARTICLE else None
    jobs = c.library.index.jobs(resource_id=resource.id, limit=20)
    duplicate_of = None
    if dup := resource.meta.get("possible_duplicate_of"):
        duplicate_of = c.library.index.row(str(dup))
    return render(
        request,
        "resource.html",
        r=resource,
        shown=shown,
        content=content,
        content_role=role,
        chunks=c.library.chunks(resource.id),
        jobs=jobs,
        duplicate_of=duplicate_of,
        notice=notice,
        error=error,
        captured=iso(resource.captured_at),
        editing=edit if who.can(ScopeLevel.WRITE) else None,
        from_attachment=from_attachment,
        extractable=extractable,
        site_warning=warning,
    )


@router.get("/r/{ref}/status")
async def resource_status(request: Request, c: Ctx, who: User, ref: str) -> Response:
    """Polled while a fetch is pending; tells HTMX to reload the page when it is done."""
    resource = _load(c, ref)
    response = render(request, "_fetch_status.html", r=resource)
    if resource.fetch_status.value in ("ready", "failed"):
        response.headers["HX-Refresh"] = "true"
    return response


@router.post("/r/{ref}/edit", dependencies=[Csrf])
async def edit(
    request: Request,
    c: Ctx,
    who: User,
    ref: str,
    title: Annotated[str | None, Form()] = None,
    why: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form()] = None,
    project: Annotated[str | None, Form()] = None,
) -> Response:
    need(who, ScopeLevel.WRITE)
    resource = _load(c, ref)
    changes = Update(
        title=title if title is not None and title != resource.title else None,
        why=why,
        notes=notes,
        tags=_split_tags(tags) if tags is not None else None,
        project=project,
    )
    try:
        resource = await c.library.update(resource.id, changes)
    except InvalidInput as exc:
        return back(f"/r/{resource.slug}", error=str(exc))
    return back(f"/r/{resource.slug}", notice="Saved.")


@router.post("/r/{ref}/status/{status}", dependencies=[Csrf])
async def set_status(c: Ctx, who: User, ref: str, status: Status) -> Response:
    need(who, ScopeLevel.WRITE)
    try:
        resource = await c.library.update(ref, Update(status=status))
    except InvalidInput as exc:
        return back(f"/r/{ref}", error=str(exc))
    return back(f"/r/{resource.slug}", notice=f"Marked as {status.value}.")


@router.post("/r/{ref}/refetch", dependencies=[Csrf])
async def refetch(c: Ctx, who: User, ref: str) -> Response:
    need(who, ScopeLevel.WRITE)
    try:
        await c.library.refetch(ref)
    except InvalidInput as exc:
        return back(f"/r/{ref}", error=str(exc))
    c.enqueued()
    return back(f"/r/{ref}", notice="Re-fetch queued.")


@router.post("/r/{ref}/attachments", dependencies=[Csrf])
async def attach(
    c: Ctx,
    who: User,
    ref: str,
    file: list[UploadFile] | None = None,
    url: Annotated[str, Form()] = "",
) -> Response:
    need(who, ScopeLevel.WRITE)
    uploads = [f for f in file or [] if f.filename]
    try:
        if uploads:
            names = []
            for upload in uploads:
                assert upload.filename
                attachment = await c.library.attach_upload(
                    ref, upload.filename, _chunks(upload), upload.content_type, who.name
                )
                names.append(attachment.path)
            notice = f"Attached {names[0]}." if len(names) == 1 else f"Attached {len(names)} files."
            return back(f"/r/{ref}", notice=notice)
        if url:
            await c.library.attach_url(ref, url, who.name)
            c.enqueued()
            return back(f"/r/{ref}", notice="Download queued.")
    except InvalidInput as exc:
        return back(f"/r/{ref}", error=str(exc))
    return back(f"/r/{ref}", error="Choose a file or give a URL.")


@router.post("/r/{ref}/attachments/{path}/extract", dependencies=[Csrf])
async def extract_attachment(c: Ctx, who: User, ref: str, path: str) -> Response:
    need(who, ScopeLevel.WRITE)
    try:
        c.library.extract_attachment(ref, path)
    except NotFound as exc:
        return back(f"/r/{ref}", error=str(exc))
    c.enqueued()
    return back(f"/r/{ref}", notice=f"Extracting the text of {path}…")


@router.post("/r/{ref}/attachments/{path}/delete", dependencies=[Csrf])
async def delete_attachment(c: Ctx, who: User, ref: str, path: str) -> Response:
    need(who, ScopeLevel.ADMIN)
    try:
        await c.library.delete_attachment(ref, path)
    except NotFound as exc:
        return back(f"/r/{ref}", error=str(exc))
    return back(f"/r/{ref}", notice=f"Deleted {path}.")


async def _chunks(file: StarletteUpload) -> AsyncIterator[bytes]:
    while block := await file.read(1 << 20):
        yield block


@router.post("/r/{ref}/delete", dependencies=[Csrf])
async def delete(c: Ctx, who: User, ref: str, confirm: Annotated[str, Form()] = "") -> Response:
    need(who, ScopeLevel.ADMIN)
    resource = _load(c, ref)
    if confirm != resource.slug:
        return back(f"/r/{resource.slug}", error="Type the slug to confirm deletion.")
    await c.library.delete(resource.id)
    return back("/", notice=f"Deleted “{resource.title}”.")


@router.get("/r/{ref}/files/{path:path}")
async def download(c: Ctx, who: User, ref: str, path: str) -> Response:
    resource = _load(c, ref)
    try:
        file_path = c.library.files.file_path(resource.id, path)
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPException(404, "No such file.") from exc
    return FileResponse(
        file_path,
        filename=file_path.name,
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
    )


# --------------------------------------------------------------------- activity


@router.get("/activity")
async def activity(request: Request, c: Ctx, who: User, status: str | None = None) -> Response:
    index = c.library.index
    jobs = index.jobs(status or None, limit=150)
    rows = index.rows(list({j.resource_id for j in jobs if j.resource_id}))
    counts = {s: len(index.jobs(s, limit=10_000)) for s in ("queued", "running", "failed", "done")}
    data: dict[str, Any] = {"jobs": jobs, "rows": rows, "status": status or "", "counts": counts}
    if request.headers.get("hx-request") and request.headers.get("hx-target") == "jobs":
        return render(request, "_jobs.html", **data)
    data["chart"] = _capture_chart(c)
    data["recent"] = c.library.search("", SearchFilters(), 8)
    return render(request, "activity.html", **data)


CHART_DAYS = 30
CHART_KINDS = ("article", "git_repo", "document", "note")


def _capture_chart(c: AppContext) -> dict[str, Any]:
    """Resources captured per day and kind over the last 30 days, as SVG-ready bars."""
    with c.library.index.read() as db:
        rows = db.execute(
            "SELECT substr(captured_at, 1, 10) AS day, kind, count(*) AS n FROM resources "
            "WHERE captured_at >= date('now', ?) GROUP BY day, kind",
            (f"-{CHART_DAYS - 1} days",),
        ).fetchall()
    today = datetime.now(UTC).date()
    days = [(today - timedelta(days=CHART_DAYS - 1 - i)).isoformat() for i in range(CHART_DAYS)]
    counts = {(r["day"], r["kind"]): r["n"] for r in rows}
    totals = [sum(counts.get((d, k), 0) for k in CHART_KINDS) for d in days]
    top = max(totals, default=0) or 1
    height, width, gap = 120.0, 18.0, 4.0
    bars = []
    for i, day in enumerate(days):
        y = height
        for kind in CHART_KINDS:
            n = counts.get((day, kind), 0)
            if not n:
                continue
            h = height * n / top
            y -= h
            bars.append({"x": i * (width + gap), "y": round(y, 1), "w": width,
                         "h": round(h, 1), "kind": kind, "n": n, "day": day})  # fmt: skip
    return {
        "bars": bars,
        "width": CHART_DAYS * (width + gap) - gap,
        "height": height,
        "total": sum(totals),
        "top": top,
        "first": days[0],
        "last": days[-1],
    }


@router.post("/jobs/{job_id}/retry", dependencies=[Csrf])
async def retry(c: Ctx, who: User, job_id: str) -> Response:
    need(who, ScopeLevel.WRITE)
    job = c.library.index.retry_job(job_id)
    c.enqueued()
    return back("/activity", notice="Job re-queued." if job else None,
                error=None if job else "That job is not failed.")  # fmt: skip


# --------------------------------------------------------------------- settings


@router.get("/settings")
async def settings_page(request: Request, c: Ctx, who: User) -> Response:
    base = c.settings.http.public_url or str(request.base_url).rstrip("/")
    bookmarklet = (
        f"javascript:location.href='{base}/add?url='+encodeURIComponent(location.href)"
        "+'&title='+encodeURIComponent(document.title)"
    )
    admin = who.can(ScopeLevel.ADMIN)
    tokens = c.tokens.list() if admin else []
    transfers = _transfers(c) if admin else {}
    return render(
        request, "settings.html", bookmarklet=bookmarklet, base=base, tokens=tokens, **transfers
    )


def _transfers(c: AppContext) -> dict[str, Any]:
    library = c.library
    active = [j for j in library.index.jobs("queued", limit=500) + library.index.jobs(
        "running", limit=500) if j.type in ("export", "import_library")]  # fmt: skip
    exports = library.exports()
    return {
        "exports": exports,
        "imports": library.import_reports(),
        "importing": [j for j in active if j.type == "import_library"],
        "busy": bool(active) or any(e.status == "running" for e in exports),
        "busy_export": any(j.type == "export" for j in active),
        "part_mb": c.settings.export.part_mb,
    }


@router.get("/settings/transfers")
async def transfers(request: Request, c: Ctx, who: User) -> Response:
    need(who, ScopeLevel.ADMIN)
    return render(request, "_transfers.html", **_transfers(c))


@router.post("/settings/export", dependencies=[Csrf])
async def start_export(c: Ctx, who: User) -> Response:
    need(who, ScopeLevel.ADMIN)
    try:
        c.library.start_export()
    except InvalidInput as exc:
        return back("/settings#library", error=str(exc))
    c.enqueued()
    return back("/settings#library", notice="Export started.")


@router.get("/settings/exports/{export_id}/{name}")
async def download_export(c: Ctx, who: User, export_id: str, name: str) -> Response:
    need(who, ScopeLevel.ADMIN)
    try:
        path = c.library.export_part(export_id, name)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="application/x-tar", filename=name)


@router.post("/settings/exports/{export_id}/delete", dependencies=[Csrf])
async def delete_export(c: Ctx, who: User, export_id: str) -> Response:
    need(who, ScopeLevel.ADMIN)
    try:
        c.library.delete_export(export_id)
    except (InvalidInput, NotFound) as exc:
        return back("/settings#library", error=str(exc))
    return back("/settings#library", notice="Export deleted.")


@router.post("/settings/import", dependencies=[Csrf])
async def import_library(c: Ctx, who: User, file: list[UploadFile] | None = None) -> Response:
    need(who, ScopeLevel.ADMIN)
    uploads = [f for f in file or [] if f.filename]
    if not uploads:
        return back("/settings#library", error="Choose one or more export parts (.tar).")
    try:
        for upload in uploads:
            assert upload.filename
            await c.library.receive_import(upload.filename, _chunks(upload))
    except InvalidInput as exc:
        return back("/settings#library", error=str(exc))
    c.enqueued()
    n = len(uploads)
    return back("/settings#library", notice=f"Importing {n} file{'s' if n > 1 else ''}…")


@router.post("/reindex", dependencies=[Csrf])
async def reindex(c: Ctx, who: User) -> Response:
    need(who, ScopeLevel.ADMIN)
    report = await c.library.reindex()
    notice = f"Reindexed {report.indexed} resources."
    if report.errors:
        notice += f" {len(report.errors)} file(s) could not be read: "
        notice += "; ".join(report.errors[:3])
    return back("/settings", notice=notice)
