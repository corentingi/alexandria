# ADR-0008 — `title_source` in the frontmatter; article slugs settle after the first fetch

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

"Title is filled from extraction unless a human has set it" requires knowing who set it.
Slugs are "derived from the title", but an article's title is unknown until it is fetched.

## Decision

- New core field `title_source: url | extracted | human`. Only a `PATCH`/UI edit of the
  title sets `human`; extraction never overwrites a `human` title. A title passed at ingest
  (the bookmarklet sends `document.title`) is provisional (`url`).
- At ingest an article gets a slug from its URL. On its *first* successful fetch the slug
  is re-derived from the extracted title. After that the slug never changes on its own.

## Why

Readable slugs (`on-building-libraries`, not `posts-on-building-libraries-blog-example-com`).

## Cost

The slug returned by `ingest` for an article may change once, seconds later. Agents should
keep the `id` (always returned, never changes). Old slugs are not kept as aliases.
