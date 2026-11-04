"""Text extraction from documents: PDF, DOCX, EPUB, HTML, plain text and Markdown.

Deterministic code only. Every extractor returns Markdown (so chunking and search work the
same as for articles) plus metadata. Inputs are untrusted: zip members are size-capped,
page and chapter counts are capped, and nothing is ever executed or fetched.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import lxml.html

log = logging.getLogger(__name__)

MAX_PAGES = 3000
MAX_CHAPTERS = 2000
MAX_ZIP_MEMBER = 50_000_000  # uncompressed bytes per member
MAX_ZIP_TOTAL = 300_000_000  # uncompressed bytes per document
MAX_MARKDOWN_CHARS = 10_000_000
MAX_HTML_BYTES = 20_000_000  # same cap as a fetched page

FORMATS = ("pdf", "docx", "epub", "html", "markdown", "text")
EXTENSIONS = {
    ".pdf": "pdf", ".docx": "docx", ".epub": "epub", ".html": "html", ".htm": "html",
    ".xhtml": "html", ".md": "markdown", ".markdown": "markdown", ".txt": "text",
    ".text": "text", ".rst": "text", ".csv": "text", ".json": "text", ".log": "text",
}  # fmt: skip
_MIMES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/epub+zip": "epub",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "text/plain": "text",
}


# Untrusted XML: no entity expansion, no network, no huge trees.
_XML_PARSER = lxml.etree.XMLParser(
    resolve_entities=False, no_network=True, huge_tree=False, load_dtd=False
)


def _xml(data: bytes) -> Any:
    return lxml.etree.fromstring(data, parser=_XML_PARSER)


class ExtractionError(Exception):
    pass


@dataclass
class Extraction:
    format: str
    markdown: str
    title: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def detect_format(path: Path, filename: str | None = None, mime: str | None = None) -> str | None:
    """Sniff the file's magic bytes first, then trust the MIME type, then the extension."""
    with path.open("rb") as fh:
        head = fh.read(2048)
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                if "word/document.xml" in names:
                    return "docx"
                if "META-INF/container.xml" in names:  # EPUB (the mimetype member is optional)
                    return "epub"
        except zipfile.BadZipFile:
            return None
        return None
    mime = (mime or "").split(";", 1)[0].strip().lower()
    by_mime = _MIMES.get(mime)
    name = (filename or path.name).lower()
    by_ext = next((fmt for ext, fmt in EXTENSIONS.items() if name.endswith(ext)), None)
    candidate = by_ext if by_mime in (None, "text") and by_ext else by_mime
    if candidate in ("html", "markdown", "text") and b"\x00" in head:
        return None  # binary data dressed up as text
    if candidate is None and head and b"\x00" not in head:
        stripped = head.lstrip().lower()
        if stripped.startswith((b"<!doctype html", b"<html")):
            return "html"
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "text"
    return candidate


def extract(path: Path, fmt: str, url: str | None = None, charset: str | None = None) -> Extraction:
    """``charset``: the encoding a server declared (``Content-Type``), for text formats."""
    try:
        if fmt in ("text", "markdown"):
            result = _TEXT_EXTRACTORS[fmt](path, url, charset)
        else:
            result = _EXTRACTORS[fmt](path, url)
    except ExtractionError:
        raise
    except Exception as exc:  # a broken file must never crash the worker
        log.warning("extraction of %s as %s failed: %s", path.name, fmt, exc)
        raise ExtractionError(f"could not read {fmt}: {exc}") from exc
    result.markdown = _tidy(result.markdown)[:MAX_MARKDOWN_CHARS]
    return result


# ---------------------------------------------------------------------------- PDF


def _pdf(path: Path, url: str | None) -> Extraction:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ExtractionError("the PDF is encrypted")
    info: Any = reader.metadata or {}
    pages = reader.pages
    parts: list[str] = []
    for number, page in enumerate(pages[:MAX_PAGES], start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # one bad page should not lose the rest
            text = ""
            log.info("page %d unreadable: %s", number, exc)
        text = _unwrap(text)
        if text.strip():
            parts.append(f"## Page {number}\n\n{text}")
    meta: dict[str, Any] = {"pages": len(pages)}
    if len(pages) > MAX_PAGES:
        meta["truncated_at_page"] = MAX_PAGES
    author = _clean(info.get("/Author"))
    if author:
        meta["author"] = author
    return Extraction("pdf", "\n\n".join(parts), _clean(info.get("/Title")), meta)


def _unwrap(text: str) -> str:
    """PDF text is hard-wrapped: join lines inside paragraphs, keep blank-line breaks."""
    text = re.sub(r"-\n(?=[a-zà-ÿ])", "", text)  # hyphenated line breaks
    paragraphs = re.split(r"\n\s*\n", text)
    return "\n\n".join(" ".join(p.split()) for p in paragraphs if p.strip())


# --------------------------------------------------------------------------- DOCX

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DC = "{http://purl.org/dc/elements/1.1/}"


def _docx(path: Path, url: str | None) -> Extraction:
    with zipfile.ZipFile(path) as zf:
        budget = _ZipBudget()
        root = _xml(budget.read(zf, "word/document.xml"))
        core = budget.read(zf, "docProps/core.xml", required=False)
    body = root.find(f"{_W}body")
    lines: list[str] = []
    for block in body if body is not None else []:
        if block.tag == f"{_W}p":
            line = _docx_paragraph(block)
            if line:
                lines.append(line)
        elif block.tag == f"{_W}tbl":
            lines.append(_docx_table(block))
    title = author = None
    if core:
        props = _xml(core)
        title = _clean(_text_of(props.find(f"{_DC}title")))
        author = _clean(_text_of(props.find(f"{_DC}creator")))
    meta = {"author": author} if author else {}
    return Extraction("docx", "\n\n".join(lines), title, meta)


def _docx_paragraph(p: Any) -> str:
    text = "".join(
        node.text or "" if node.tag == f"{_W}t" else ("\t" if node.tag == f"{_W}tab" else "\n")
        for node in p.iter()
        if node.tag in (f"{_W}t", f"{_W}tab", f"{_W}br", f"{_W}cr")
    ).strip()
    if not text:
        return ""
    props = p.find(f"{_W}pPr")
    style = ""
    if props is not None:
        style_el = props.find(f"{_W}pStyle")
        style = (style_el.get(f"{_W}val") or "") if style_el is not None else ""
        if props.find(f"{_W}numPr") is not None:
            return f"- {text}"
    match = re.match(r"(?i)^heading\s*(\d)$", style)
    if match:
        return "#" * min(int(match.group(1)), 6) + " " + text
    if style.lower() == "title":
        return f"# {text}"
    return text


def _docx_table(table: Any) -> str:
    rows = []
    for tr in table.iter(f"{_W}tr"):
        cells = [
            " ".join(filter(None, map(_docx_paragraph, tc.iter(f"{_W}p")))).replace("|", "/")
            for tc in tr.iter(f"{_W}tc")
        ]
        rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return ""
    width = rows[0].count("|") - 1
    return "\n".join([rows[0], "|" + " --- |" * width, *rows[1:]])


# --------------------------------------------------------------------------- EPUB

_OPF = "{http://www.idpf.org/2007/opf}"
_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"


def _epub(path: Path, url: str | None) -> Extraction:
    with zipfile.ZipFile(path) as zf:
        budget = _ZipBudget()
        container = _xml(budget.read(zf, "META-INF/container.xml"))
        rootfile = container.find(f".//{_CONTAINER}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise ExtractionError("EPUB has no package document")
        opf_path = PurePosixPath(rootfile.get("full-path", ""))
        opf = _xml(budget.read(zf, str(opf_path)))
        metadata = opf.find(f"{_OPF}metadata")
        title = author = language = None
        if metadata is not None:
            title = _clean(_text_of(metadata.find(f"{_DC}title")))
            author = _clean(_text_of(metadata.find(f"{_DC}creator")))
            language = _clean(_text_of(metadata.find(f"{_DC}language")))
        manifest = {
            item.get("id"): item.get("href")
            for item in opf.iter(f"{_OPF}item")
            if "html" in (item.get("media-type") or "")
        }
        spine = [ref.get("idref") for ref in opf.iter(f"{_OPF}itemref")]
        chapters: list[str] = []
        for idref in spine[:MAX_CHAPTERS]:
            href = manifest.get(idref)
            if not href:
                continue
            member = _resolve(opf_path.parent, href)
            raw = budget.read(zf, member, required=False)
            if raw:
                chapters.append(html_to_markdown(raw))
    meta: dict[str, Any] = {"chapters": len(chapters)}
    if author:
        meta["author"] = author
    if language:
        meta["language"] = language.split("-")[0].lower()
    return Extraction("epub", "\n\n".join(c for c in chapters if c.strip()), title, meta)


def _resolve(base: PurePosixPath, href: str) -> str:
    parts: list[str] = []
    for part in (base / href.split("#", 1)[0]).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts)


class _ZipBudget:
    """Caps what a single document may decompress (zip bombs)."""

    def __init__(self) -> None:
        self.used = 0

    def read(self, zf: zipfile.ZipFile, name: str, required: bool = True) -> bytes:
        try:
            info = zf.getinfo(name)
        except KeyError:
            if required:
                raise ExtractionError(f"missing {name}") from None
            return b""
        if info.file_size > MAX_ZIP_MEMBER or self.used + info.file_size > MAX_ZIP_TOTAL:
            raise ExtractionError("document is too large once decompressed")
        with zf.open(info) as fh:
            data = fh.read(MAX_ZIP_MEMBER + 1)
        if len(data) > MAX_ZIP_MEMBER:
            raise ExtractionError("document is too large once decompressed")
        self.used += len(data)
        return data


# --------------------------------------------------------------------------- HTML


def _html(path: Path, url: str | None) -> Extraction:
    import trafilatura

    raw = _head(path, MAX_HTML_BYTES)
    markdown = trafilatura.extract(
        raw, url=url, output_format="markdown", include_links=True, include_tables=True,
        include_comments=False, include_formatting=True,
    )  # fmt: skip
    if not markdown or len(markdown) < 200:
        markdown = html_to_markdown(raw)  # trafilatura drops pages it deems boilerplate
    title = None
    try:
        tree = lxml.html.fromstring(raw)
        title = _clean(tree.findtext(".//title"))
    except ValueError, lxml.etree.ParserError:
        pass
    return Extraction("html", markdown or "", title)


_BLOCKS = {
    "p", "div", "section", "article", "blockquote", "pre", "li", "tr", "figcaption", "dd",
    "dt", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "br", "hr",
}  # fmt: skip
_SKIP = {"script", "style", "head", "nav", "noscript", "svg", "template"}


def html_to_markdown(raw: bytes | str) -> str:
    """A small, predictable HTML → Markdown conversion (headings, paragraphs, lists, code)."""
    try:
        tree = lxml.html.fromstring(raw)
    except ValueError, lxml.etree.ParserError:
        return ""
    out: list[str] = []

    def walk(el: Any, list_depth: int = 0) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        tag = tag.rsplit("}", 1)[-1].lower()
        if tag in _SKIP:
            if el.tail:
                out.append(el.tail)
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = " ".join(el.text_content().split())
            if text:
                out.append(f"\n\n{'#' * int(tag[1])} {text}\n\n")
        elif tag == "pre":
            out.append(f"\n\n```\n{el.text_content().strip(chr(10))}\n```\n\n")
        elif tag == "li":
            out.append("\n" + "  " * max(0, list_depth - 1) + "- ")
            _children(el, list_depth)
            out.append("\n")
        else:
            if tag in _BLOCKS:
                out.append("\n\n" if tag not in ("br",) else "\n")
            if tag == "hr":
                out.append("---\n\n")
            _children(el, list_depth + (1 if tag in ("ul", "ol") else 0))
            if tag in _BLOCKS:
                out.append("\n\n")
        if el.tail:
            out.append(el.tail)

    def _children(el: Any, depth: int) -> None:
        if el.text:
            out.append(el.text)
        for child in el:
            walk(child, depth)

    body = tree.find(".//body")
    walk(body if body is not None else tree)
    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


# --------------------------------------------------------------------------- text


def _text(path: Path, url: str | None, charset: str | None = None) -> Extraction:
    raw = _head(path, MAX_MARKDOWN_CHARS * 2)
    text = _decode(raw, charset)
    first = next((line.strip("# ").strip() for line in text.splitlines() if line.strip()), "")
    return Extraction("text", text, first[:200] or None)


def _markdown(path: Path, url: str | None, charset: str | None = None) -> Extraction:
    result = _text(path, url, charset)
    result.format = "markdown"
    match = re.search(r"^#\s+(.+)$", result.markdown, flags=re.MULTILINE)
    if match:
        result.title = match.group(1).strip()
    return result


_ASCII = bytes(range(0x80))


def _decode(raw: bytes, charset: str | None = None) -> str:
    """A BOM, else the declared charset, else UTF-8, else Windows-1252.

    UTF-8 survives what used to garble a whole file: a multibyte character cut by the
    size cap, and a few stray invalid bytes (replaced, not reinterpreted). Only text
    that is mostly not UTF-8 is read as Windows-1252.
    """
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if charset:
        try:
            return raw.decode(charset.strip().strip('"'), errors="replace")
        except LookupError:
            pass  # an unknown charset name: sniff instead
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        if exc.start >= len(raw) - 3:  # only the cut at the end is incomplete
            return raw[: exc.start].decode("utf-8-sig")
    # Mostly valid UTF-8 (a stray byte among many accented characters) stays UTF-8; text
    # whose non-ASCII bytes are mostly invalid as UTF-8 is a legacy encoding.
    text = raw.decode("utf-8-sig", errors="replace")
    non_ascii = len(raw.translate(None, _ASCII))
    if text.count("\ufffd") * 20 <= non_ascii:
        return text
    return raw.decode("cp1252", errors="replace")


# --------------------------------------------------------------------------- helpers

_EXTRACTORS = {"pdf": _pdf, "docx": _docx, "epub": _epub, "html": _html}
_TEXT_EXTRACTORS = {"markdown": _markdown, "text": _text}


def _head(path: Path, limit: int) -> bytes:
    """At most ``limit`` bytes: a huge upload must not be read whole into memory."""
    with path.open("rb") as fh:
        return fh.read(limit)


def _text_of(el: Any) -> str | None:
    return el.text if el is not None else None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _tidy(markdown: str) -> str:
    markdown = markdown.replace("\r\n", "\n").replace("\x00", "")
    return re.sub(r"\n{3,}", "\n\n", markdown).strip()
