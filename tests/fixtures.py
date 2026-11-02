"""Tiny, valid documents built in code, so tests need no binary fixtures."""

from __future__ import annotations

import io
import zipfile


def make_pdf(pages: list[str], title: str = "Quarterly Report") -> bytes:
    """A minimal PDF with one Helvetica text line per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    info_id = len(objects) + 1
    objects.append(f"<< /Title ({title}) /Author (Ada Lovelace) >>".encode())

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {info_id} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(document_xml_body: str | None = None) -> bytes:
    body = document_xml_body or (
        '<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>Design Notes</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Storage</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>We keep files on disk and an </w:t></w:r>"
        "<w:r><w:t>index in SQLite.</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr>'
        "<w:r><w:t>first bullet</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Key</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>engine</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>fts5</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", f"<w:document {_W}><w:body>{body}</w:body></w:document>")
        zf.writestr(
            "docProps/core.xml",
            '<cp:coreProperties xmlns:cp="x" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Design Notes</dc:title><dc:creator>Grace Hopper</dc:creator>"
            "</cp:coreProperties>",
        )
    return buf.getvalue()


def make_epub() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>The Lighthouse Keeper</dc:title>'
            "<dc:creator>Jules Verne</dc:creator><dc:language>fr-FR</dc:language></metadata>"
            '<manifest><item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c2"/><itemref idref="c1"/></spine></package>',
        )
        zf.writestr(
            "OEBPS/text/ch1.xhtml",
            "<html><body><h1>Chapter One</h1><p>The lamp burned all night.</p></body></html>",
        )
        zf.writestr(
            "OEBPS/text/ch2.xhtml",
            "<html><body><h1>Prologue</h1><p>A storm over the island.</p>"
            "<ul><li>wind</li><li>rain</li></ul></body></html>",
        )
    return buf.getvalue()


def make_zip_bomb() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"<a>" + b" " * 60_000_000 + b"</a>")
    return buf.getvalue()


XXE_DOCX_BODY = None  # built in the test: needs a DOCTYPE before the root element


def make_xxe_docx() -> bytes:
    xml = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY xxe SYSTEM "file:///etc/passwd">'
        '<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        f"<w:document {_W}><w:body><w:p><w:r><w:t>&xxe;&b;safe</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()
