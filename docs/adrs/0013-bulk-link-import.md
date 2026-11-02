# ADR-0013 — Bulk import of links from text or a bookmarks export

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

The owner asked for ingestion of "basic text formats like urls". Read as: getting existing
link collections in (a pasted list, a browser bookmarks export).

## Decision

`Library.import_urls(text, why?, tags?, project?)` collects every `http(s)` link in the
text: `<a href>` anchors (keeping the `TAGS` attribute of Netscape-format bookmark exports
and the link text as a provisional title) plus bare URLs. Each goes through the normal
`ingest` (canonicalization, duplicate check, SSRF guard); the report lists created ids,
duplicates and rejections. At most 5,000 links per import. Exposed as `POST /api/v1/import`,
the UI's "Import links" tab and `alexandria import <file>`. Not exposed through MCP: an
agent can call `ingest_url` per link and give each its own `why`.

## Cost

Imported links usually have no `why`, so a large import fills the inbox. That is the
intended pressure (principle 4); the shared `why` field exists for batches with one reason.
