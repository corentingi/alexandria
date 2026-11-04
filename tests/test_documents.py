from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx

from alexandria.extract import ExtractionError, _decode, detect_format, extract
from alexandria.fetchers.document import charset_of
from alexandria.jobs import Worker
from alexandria.library import Library, Update, parse_url_list
from alexandria.models import FetchStatus, Kind, Status
from tests.fixtures import make_docx, make_epub, make_pdf, make_xxe_docx, make_zip_bomb


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


async def stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


class TestExtract:
    def test_pdf(self, tmp_path: Path) -> None:
        path = write(tmp_path, "r.pdf", make_pdf(["Revenue grew by ten percent", "Outlook stable"]))
        assert detect_format(path) == "pdf"
        result = extract(path, "pdf")
        assert result.title == "Quarterly Report"
        assert result.meta == {"pages": 2, "author": "Ada Lovelace"}
        assert "## Page 1\n\nRevenue grew by ten percent" in result.markdown
        assert "## Page 2" in result.markdown

    def test_docx(self, tmp_path: Path) -> None:
        path = write(tmp_path, "notes.docx", make_docx())
        assert detect_format(path) == "docx"
        result = extract(path, "docx")
        assert result.title == "Design Notes"
        assert result.meta["author"] == "Grace Hopper"
        md = result.markdown
        assert md.startswith("# Design Notes\n\n# Storage")
        assert "We keep files on disk and an index in SQLite." in md
        assert "- first bullet" in md
        assert "| engine | fts5 |" in md

    def test_epub_follows_spine_order(self, tmp_path: Path) -> None:
        path = write(tmp_path, "book.epub", make_epub())
        assert detect_format(path) == "epub"
        result = extract(path, "epub")
        assert result.title == "The Lighthouse Keeper"
        assert result.meta == {"chapters": 2, "author": "Jules Verne", "language": "fr"}
        assert result.markdown.index("Prologue") < result.markdown.index("Chapter One")
        assert "- wind" in result.markdown

    def test_text_markdown_html(self, tmp_path: Path) -> None:
        md = write(tmp_path, "a.md", b"Intro\n\n# Real Title\n\nbody")
        assert detect_format(md) == "markdown"
        assert extract(md, "markdown").title == "Real Title"
        txt = write(tmp_path, "a.txt", "café au lait\n".encode("cp1252"))
        assert detect_format(txt) == "text"
        assert "café" in extract(txt, "text").markdown
        html = write(tmp_path, "page.html", b"<html><head><title>T</title></head>"
                     b"<body><h2>Sub</h2><p>Hello there</p></body></html>")  # fmt: skip
        result = extract(html, "html")
        assert result.title == "T" and "## Sub" in result.markdown

    def test_binary_is_not_text(self, tmp_path: Path) -> None:
        path = write(tmp_path, "x.txt", b"\x89PNG\r\n\x1a\n\x00\x00")
        assert detect_format(path) is None

    def test_zip_bomb_refused(self, tmp_path: Path) -> None:
        path = write(tmp_path, "bomb.docx", make_zip_bomb())
        with pytest.raises(ExtractionError, match="too large"):
            extract(path, "docx")

    def test_xml_entities_not_expanded(self, tmp_path: Path) -> None:
        path = write(tmp_path, "xxe.docx", make_xxe_docx())
        try:
            result = extract(path, "docx")
        except ExtractionError:
            return  # refusing the file is fine too
        assert "root:" not in result.markdown
        assert "aaaaaaaaaa" not in result.markdown

    def test_corrupt_pdf(self, tmp_path: Path) -> None:
        path = write(tmp_path, "bad.pdf", b"%PDF-1.4\n garbage")
        with pytest.raises(ExtractionError):
            extract(path, "pdf")


class TestDecoding:
    def test_utf8_survives_a_cut_and_stray_bytes(self) -> None:
        cut = "Le café".encode()[:-1]  # the size cap split "é", a two-byte character
        assert _decode(cut) == "Le caf"
        stray = ("Le café est prêt. " * 200).encode() + b"\xff" + "déjà vu".encode()
        decoded = _decode(stray)
        assert "café" in decoded and "déjà vu" in decoded and decoded.count("\ufffd") == 1

    def test_legacy_text_and_declared_charsets(self) -> None:
        assert _decode("Crème brûlée".encode("cp1252")) == "Crème brûlée"
        assert _decode("日本語のテキスト".encode("shift_jis"), "shift_jis") == "日本語のテキスト"
        assert _decode(b"plain", "no-such-charset") == "plain"
        assert charset_of('text/plain; charset="Shift_JIS"') == "Shift_JIS"
        assert charset_of("text/plain") is None

    async def test_fetched_text_uses_the_server_charset(
        self, library: Library, worker: Worker
    ) -> None:
        url = "https://files.example/notes.txt"
        body = "日本語のテキスト、長い文章。".encode("shift_jis")
        with respx.mock() as router:
            router.get(url).mock(return_value=httpx.Response(
                200, content=body, headers={"content-type": "text/plain; charset=shift_jis"}
            ))  # fmt: skip
            result = await library.ingest(url, why="x")
            await worker.drain()
        assert "日本語のテキスト" in library.content(result.resource.id)["text"]


class TestDocuments:
    async def test_upload_extract_search(self, library: Library, worker: Worker) -> None:
        data = make_pdf(["The heron nests near the estuary", "Migration patterns"])
        result = await library.ingest_file("field-notes_2026.pdf", stream(data), "application/pdf",
                                           why="bird survey", tags=["birds"])  # fmt: skip
        r = result.resource
        assert r.kind == Kind.DOCUMENT and r.status == Status.KEPT
        assert r.title == "field notes 2026"
        assert r.canonical_url and r.canonical_url.startswith("urn:sha256:")
        assert library.files.file_path(r.id, "source/field-notes_2026.pdf").read_bytes() == data
        await worker.drain()

        res = library.get(r.id)
        assert res.fetch_status == FetchStatus.READY, res.fetch_error
        assert res.title == "Quarterly Report"  # from the PDF metadata
        assert res.meta["format"] == "pdf" and res.meta["pages"] == 2
        assert {a.role for a in res.snapshot().artifacts} == {"content", "extract"}  # type: ignore[union-attr]
        hits = library.search("heron estuary")
        assert [row["id"] for _, row in hits] == [r.id]
        assert hits[0][0].heading_path == "Page 1"
        assert "heron" in library.content(r.id, chunk=0)["text"]

        dup = await library.ingest_file("copy.pdf", stream(data))
        assert dup.duplicate and dup.resource.id == r.id

    async def test_unsupported_file_is_kept(self, library: Library, worker: Worker) -> None:
        result = await library.ingest_file("photo.png", stream(b"\x89PNG\r\n\x1a\n\x00\x00"))
        await worker.drain()
        res = library.get(result.resource.id)
        assert res.fetch_status == FetchStatus.READY
        assert res.meta["extraction"] == "unsupported"
        assert res.status == Status.INBOX
        assert res.source_file and res.source_file.path == "photo.png"

    async def test_reextract_and_reindex(self, library: Library, worker: Worker) -> None:
        result = await library.ingest_file("book.epub", stream(make_epub()))
        await worker.drain()
        await library.refetch(result.resource.id)
        await worker.drain()
        res = library.get(result.resource.id)
        assert len(res.snapshots) == 2
        await library.update(res.id, Update(why="bedtime reading"))
        before = [(row["id"], h.snippet) for h, row in library.search("lamp")]
        report = await library.reindex()
        assert report.errors == []
        assert [(row["id"], h.snippet) for h, row in library.search("lamp")] == before != []

    async def test_delete_snapshots(self, library: Library, worker: Worker) -> None:
        from alexandria.library import InvalidInput, NotFound

        result = await library.ingest_file("book.epub", stream(make_epub()))
        await worker.drain()
        await library.refetch(result.resource.id)
        await worker.drain()
        res = library.get(result.resource.id)
        first, second = (s.id for s in res.snapshots)
        assert res.current_snapshot == second

        res = await library.delete_snapshot(res.id, first)  # an old one
        assert [s.id for s in res.snapshots] == [second] and res.current_snapshot == second
        assert not library.files.snapshot_dir(res.id, first).exists()
        assert [row["id"] for _, row in library.search("lamp")] == [res.id]
        with pytest.raises(InvalidInput, match="only snapshot"):
            await library.delete_snapshot(res.id, second)
        with pytest.raises(NotFound):
            await library.delete_snapshot(res.id, ".")

        await library.refetch(res.id)
        await worker.drain()
        third = library.get(res.id).current_snapshot
        res = await library.delete_snapshot(res.id, third)  # the current one
        assert res.current_snapshot == second
        assert [row["id"] for _, row in library.search("lamp")] == [res.id]

    async def test_hostile_snapshot_id(self, library: Library) -> None:
        from alexandria.library import InvalidInput
        from alexandria.models import Snapshot

        note = await library.create_note("N", "x")
        resource = library.files.read(note.id)
        resource.snapshots = [Snapshot(id="."), Snapshot(id="20261103T000000Z")]
        library.files.write(resource)
        (library.files.resource_dir(note.id) / "snapshots" / "keep").mkdir(parents=True)
        with pytest.raises(InvalidInput, match="not a snapshot id"):
            await library.delete_snapshot(note.id, ".")
        assert (library.files.resource_dir(note.id) / "snapshots" / "keep").exists()

    async def test_empty_upload_rejected(self, library: Library) -> None:
        from alexandria.library import InvalidInput

        with pytest.raises(InvalidInput):
            await library.ingest_file("x.txt", stream(b""))

    async def test_pdf_url_is_extracted(self, library: Library, worker: Worker) -> None:
        with respx.mock() as router:
            router.get("https://papers.example/heron.pdf").mock(
                return_value=httpx.Response(
                    200,
                    content=make_pdf(["Herons and egrets"]),
                    headers={"content-type": "application/pdf"},
                )
            )
            result = await library.ingest("https://papers.example/heron.pdf")
            await worker.drain()
        res = library.get(result.resource.id)
        assert res.kind == Kind.ARTICLE
        assert {a.role for a in res.snapshot().artifacts} == {
            "original",
            "content",
            "extract",
            "fetch",
        }  # type: ignore[union-attr]
        assert [row["id"] for _, row in library.search("egrets")] == [res.id]


class TestImport:
    def test_parse_plain_text(self) -> None:
        text = "see https://a.example/x, and (https://b.example/y).\nhttps://a.example/x again"
        assert [u for u, _, _ in parse_url_list(text)] == [
            "https://a.example/x",
            "https://b.example/y",
        ]

    def test_parse_bookmarks_export(self) -> None:
        html = """<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><p>
        <DT><A HREF="https://c.example/" ADD_DATE="1" TAGS="rust,cli">C tool</A>
        <DT><A HREF="javascript:alert(1)">bad</A>
        <DT><A HREF="https://d.example/">D</A></DL>"""
        assert parse_url_list(html) == [
            ("https://c.example/", ["rust", "cli"], "C tool"),
            ("https://d.example/", [], "D"),
        ]

    def test_parse_mixed_html_and_text(self) -> None:
        text = '<a href="https://c.example/">C</a>\nand https://e.example/page'
        assert [u for u, _, _ in parse_url_list(text)] == [
            "https://c.example/",
            "https://e.example/page",
        ]

    async def test_import(self, library: Library) -> None:
        await library.ingest("https://a.example/x")
        report = await library.import_urls(
            "https://a.example/x\nhttps://b.example/y\nhttp://localhost/z", tags=["imported"]
        )
        assert len(report.created) == 1 and len(report.duplicates) == 1
        assert len(report.errors) == 1 and "SSRF" in report.errors[0]
        assert library.get(report.created[0]).tags == ["imported"]
