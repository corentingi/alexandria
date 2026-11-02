# ADR-0009 — A note's body is its content, indexed once

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

For articles and repos the `resource.md` body holds human notes (weight ×2 in search). For
a `note` the body *is* the content, also chunked (×1). Indexing it in both places made any
word in a note body outrank a note whose title matched.

## Decision

For `kind: note`, the body is indexed only through chunks; the `notes` FTS column is empty.
The API does not repeat it as `notes` in `get_resource`; `get_content` returns it.

## Why

Title matches (×5) must win over body matches, as the design's weights intend.

## Cost

None observed.
