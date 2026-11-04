"""REST API, ``/api/v1`` (design §10.2). Bearer tokens only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from alexandria.auth import Principal, ScopeLevel, bearer
from alexandria.context import AppContext
from alexandria.library import Update
from alexandria.models import Kind, Scope, Status
from alexandria.storage.index import SearchFilters
from alexandria.views import job_view, resource_detail, search_result
from alexandria.walls import site_warning

router = APIRouter(prefix="/api/v1")


def ctx(request: Request) -> AppContext:
    context: AppContext = request.app.state.ctx
    return context


def principal(request: Request) -> Principal:
    found = ctx(request).tokens.verify(bearer(request.headers.get("authorization")))
    if found is None:
        raise HTTPException(401, "missing or invalid bearer token")
    return found


def require(level: ScopeLevel) -> Any:
    def check(who: Annotated[Principal, Depends(principal)]) -> Principal:
        if not who.can(level):
            raise HTTPException(403, f"this needs the {level.label} scope")
        return who

    return Depends(check)


Reader = Annotated[Principal, require(ScopeLevel.READ)]
Writer = Annotated[Principal, require(ScopeLevel.WRITE)]
Admin = Annotated[Principal, require(ScopeLevel.ADMIN)]
Ctx = Annotated[AppContext, Depends(ctx)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestBody(_Strict):
    url: str
    why: str | None = None
    tags: list[str] = Field(default_factory=list)
    project: str | None = None
    scope: Scope | None = None
    kind: Kind | None = None
    title: str | None = None


class NoteBody(_Strict):
    title: str
    body: str = ""
    why: str | None = None
    tags: list[str] = Field(default_factory=list)
    project: str | None = None
    scope: Scope | None = None


class PatchBody(_Strict):
    why: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    status: Status | None = None
    title: str | None = None
    project: str | None = None
    scope: Scope | None = None


class AttachUrlBody(_Strict):
    url: str


def _detail(c: AppContext, ref: str) -> dict[str, Any]:
    resource = c.library.get(ref)
    return resource_detail(resource, c.library.chunks(resource.id))


@router.post("/resources", status_code=202)
async def ingest(body: IngestBody, c: Ctx, who: Writer) -> JSONResponse:
    result = await c.library.ingest(
        body.url,
        why=body.why,
        tags=body.tags,
        scope=body.scope,
        project=body.project,
        kind=body.kind,
        title=body.title,
        created_by=who.name,
    )
    c.enqueued()
    r = result.resource
    payload = {
        "id": r.id,
        "slug": r.slug,
        "status": r.status.value,
        "fetch_status": r.fetch_status.value,
        "duplicate": result.duplicate,
        **({"added": result.added} if result.added else {}),
    }
    if warning := site_warning(r.canonical_url or body.url):
        payload["warning"] = warning.message
    return JSONResponse(payload, status_code=200 if result.duplicate else 202)


@router.post("/documents", status_code=202)
async def upload_document(
    c: Ctx,
    who: Writer,
    file: Annotated[UploadFile, File()],
    why: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form(description="comma or space separated")] = None,
    project: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """Upload a PDF, DOCX, EPUB, HTML, Markdown or text file (any file is kept)."""
    result = await c.library.ingest_file(
        file.filename or "document",
        _chunks(file),
        file.content_type,
        why=why,
        tags=(tags or "").replace(",", " ").split(),
        project=project,
        created_by=who.name,
    )
    c.enqueued()
    r = result.resource
    payload = {"id": r.id, "slug": r.slug, "status": r.status.value,
               "fetch_status": r.fetch_status.value, "duplicate": result.duplicate}  # fmt: skip
    return JSONResponse(payload, status_code=200 if result.duplicate else 202)


class ImportBody(_Strict):
    text: str
    why: str | None = None
    tags: list[str] = Field(default_factory=list)
    project: str | None = None


@router.post("/import")
async def import_urls(body: ImportBody, c: Ctx, who: Writer) -> dict[str, Any]:
    """Ingest every URL in a text, or in a browser bookmarks export (HTML)."""
    report = await c.library.import_urls(body.text, body.why, body.tags, body.project, who.name)
    c.enqueued()
    return {"created": report.created, "duplicates": report.duplicates, "errors": report.errors}


@router.post("/notes", status_code=201)
async def create_note(body: NoteBody, c: Ctx, who: Writer) -> dict[str, Any]:
    note = await c.library.create_note(
        body.title, body.body, body.why, body.tags, body.scope, body.project, who.name
    )
    return _detail(c, note.id)


@router.get("/resources/{ref}")
async def get_resource(ref: str, c: Ctx, _: Reader) -> dict[str, Any]:
    return _detail(c, ref)


@router.patch("/resources/{ref}")
async def patch_resource(ref: str, body: PatchBody, c: Ctx, _: Writer) -> dict[str, Any]:
    resource = await c.library.update(ref, Update(**body.model_dump()))
    return _detail(c, resource.id)


@router.delete("/resources/{ref}", status_code=204)
async def delete_resource(ref: str, c: Ctx, _: Admin) -> None:
    await c.library.delete(ref)


@router.delete("/resources/{ref}/snapshots/{snapshot_id}", status_code=204)
async def delete_snapshot(ref: str, snapshot_id: str, c: Ctx, _: Admin) -> None:
    await c.library.delete_snapshot(ref, snapshot_id)


@router.delete("/resources/{ref}/attachments/{path}", status_code=204)
async def delete_attachment(ref: str, path: str, c: Ctx, _: Admin) -> None:
    await c.library.delete_attachment(ref, path)


@router.post("/resources/{ref}/refetch", status_code=202)
async def refetch(ref: str, c: Ctx, _: Writer) -> dict[str, Any]:
    job_id = await c.library.refetch(ref)
    c.enqueued()
    return {"job_id": job_id}


@router.post("/resources/{ref}/attachments", status_code=201)
async def attach(
    ref: str,
    request: Request,
    c: Ctx,
    who: Writer,
    file: Annotated[UploadFile | None, File()] = None,
) -> JSONResponse:
    if file is not None:
        attachment = await c.library.attach_upload(
            ref, file.filename or "file", _chunks(file), file.content_type, who.name
        )
        return JSONResponse(attachment.model_dump(mode="json"), status_code=201)
    try:
        body = AttachUrlBody.model_validate(await request.json())
    except ValueError as exc:
        raise HTTPException(400, "send a multipart 'file' or a JSON body {url}") from exc
    job_id = await c.library.attach_url(ref, body.url, who.name)
    c.enqueued()
    return JSONResponse({"job_id": job_id}, status_code=202)


async def _chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while block := await file.read(1 << 20):
        yield block


@router.get("/resources/{ref}/content")
async def content(
    ref: str,
    c: Ctx,
    _: Reader,
    chunk: int | None = None,
    offset: int = 0,
    max_chars: int = 8000,
) -> dict[str, Any]:
    resource = c.library.get(ref)
    return {"source_url": resource.canonical_url} | c.library.content(ref, chunk, offset, max_chars)


@router.get("/resources/{ref}/files/{path:path}")
async def download(ref: str, path: str, c: Ctx, _: Reader) -> FileResponse:
    resource_id = c.library.resolve(ref)
    try:
        file_path = c.library.files.file_path(resource_id, path)
    except (FileNotFoundError, PermissionError) as exc:
        raise HTTPException(404, "no such file") from exc
    # Served as a download, never rendered: fetched content is untrusted.
    return FileResponse(
        file_path,
        filename=file_path.name,
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"},
    )


@router.get("/search")
async def search(
    c: Ctx,
    _: Reader,
    q: str = "",
    kind: Kind | None = None,
    tag: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI copies defaults
    project: str | None = None,
    scope: Scope | None = None,
    status: Status | None = None,
    captured_after: str | None = None,
    captured_before: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    filters = SearchFilters(
        kind=kind.value if kind else None,
        tags=tag,
        project=project,
        scope=scope.value if scope else None,
        status=status.value if status else None,
        captured_after=captured_after,
        captured_before=captured_before,
    )
    hits = c.library.search(q, filters, limit, offset)
    return {"results": [search_result(h, r) for h, r in hits]}


@router.get("/tags")
async def tags(c: Ctx, _: Reader) -> dict[str, Any]:
    return {"tags": [{"tag": t, "count": n} for t, n in c.library.tags()]}


@router.get("/jobs")
async def jobs(c: Ctx, _: Reader, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    return {"jobs": [job_view(j) for j in c.library.index.jobs(status, limit=limit)]}


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, c: Ctx, _: Writer) -> dict[str, Any]:
    job = c.library.index.retry_job(job_id)
    if job is None:
        raise HTTPException(404, "no failed job with this id")
    c.enqueued()
    return job_view(job)


@router.post("/reindex")
async def reindex(c: Ctx, _: Admin, ref: str | None = None) -> dict[str, Any]:
    report = await c.library.reindex(ref)
    return {"indexed": report.indexed, "errors": report.errors}
