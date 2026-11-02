# ADR-0012 — Documents: an uploaded original in `source/`, text extracted into snapshots

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

The owner asked for ingestion of files (PDF, DOCX, ebooks, text), "or at least" keeping
them as artifacts to analyse later. The design lists PDF text extraction as a v1 non-goal
and has no kind for a file without a URL.

## Decision

- New kind **`document`**. The uploaded file is stored once, unchanged, in
  `resources/<id>/source/<sanitized name>` and described by a new core field
  `source_file` (same shape as an attachment). Its `canonical_url` is
  `urn:sha256:<hash>`, so uploading the same bytes twice returns the existing resource with
  `duplicate: true` (the same rule as URLs, through the same unique index).
- The "fetch" job of a document is an **extraction**: `fetchers/document.py` writes
  `content.md` (Markdown) and `extract.json` into a new snapshot. `refetch` re-extracts,
  so a better extractor can be applied to old files later.
- Extractors (`extract.py`): PDF via `pypdf` (one `## Page N` section per page, so chunks
  and agent reads follow pages), DOCX and EPUB parsed directly from their zip/XML (headings,
  lists, tables; EPUB in spine order), HTML via trafilatura with a fallback converter,
  Markdown and text (UTF-8, UTF-16 or cp1252).
- The same extraction runs on **URLs that serve these formats** (a link to a PDF): the
  article snapshot keeps `original.<ext>` and gains `content.md` and `extract.json`.
- A file that cannot be read never fails the resource: the original stays, flagged
  `meta.extraction: unsupported | failed` (with `extraction_error`), or `poor` for a PDF or
  EPUB that yields almost no text (likely a scan; OCR is future work).
- Untrusted input: zip members and totals are capped (zip bombs), XML is parsed by lxml with
  entity resolution, DTD loading and network access disabled, page and chapter counts are
  capped, uploads obey `attachments.max_mb`.

## Why

- Keeping the original untouched and deriving text in snapshots is principle 1 applied to
  files: extraction is derived data and can be redone.
- One code path for "file from upload" and "file from URL".
- `pypdf` is pure Python (no system libraries in the image); DOCX/EPUB need no new
  dependency (lxml comes with trafilatura).

## Cost

- One dependency (`pypdf`, ~300 KB).
- No OCR, no layout fidelity (columns, footnotes, tables in PDFs come out as plain text).
- Extraction of a very large PDF runs in a worker thread that cannot be interrupted.
- The design's §3 non-goal "PDF text extraction" is reversed for v1.
