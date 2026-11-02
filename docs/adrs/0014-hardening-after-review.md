# ADR-0014 — Hardening after the first review

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

An independent review of the PoC found ways around guarantees the design makes (§12).

## Decision

- **git never follows HTTP redirects** (`http.followRedirects=false` through
  `GIT_CONFIG_*`): a redirect would send git to a host the SSRF guard never checked.
- **Clones are size-capped while they run**: a watchdog kills `git clone` once the bare
  repository passes `git.max_archive_mb`; the resource keeps its metadata and README with
  `meta.archive_skipped: too_large` (design §8.3), instead of retrying for another 15 min.
- **The git process is killed on every exit path** (timeout, cancellation at shutdown).
- **Writes are authenticated before their body is read** (`app.RequestGuard`): FastAPI
  parses multipart bodies before auth dependencies run, so anonymous clients could make the
  server spool uploads to disk. POST/PUT/PATCH/DELETE need a valid bearer token (`/api/`) or
  session cookie (UI), except `/login`. Bodies above `attachments.max_mb` + 25 MB get 413.
  Routes still check scopes and CSRF.
- **Snapshot installation is failure-safe**: an error after the move removes the new
  snapshot folder and records `fetch_error`, so no resource stays `fetching`.
- **Agents see extracted metadata as `untrusted_meta`**, and the MCP instructions say that
  extracted titles and headings are untrusted too.
- Note bodies are stripped on the way in, as python-frontmatter does on the way out, so
  live and reindexed chunks match.

## Cost

- A repository that redirects (renamed GitHub repos do) fails to clone by its old URL;
  GitHub clones use the `clone_url` returned by the API, which is current.
- An expired session posting a form gets a bare 401 instead of a redirect to the login page.
