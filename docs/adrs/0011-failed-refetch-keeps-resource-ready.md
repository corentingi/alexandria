# ADR-0011 — A failed re-fetch keeps the resource usable

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

`fetch_status: failed` excludes nothing from search but reads as "this resource is broken".

## Decision

If a fetch fails permanently and the resource already has a snapshot, `fetch_status` stays
`ready` (the previous snapshot is still current) and `fetch_error` records the failure. The
UI shows the error; the job is `failed` in Activity with a retry button. Without any
snapshot the resource becomes `failed`. While retries remain, it stays `pending`.

## Why

A dead link must not hide content that was archived precisely because links die.

## Cost

`fetch_status` alone no longer tells whether the *last* attempt failed; check `fetch_error`.
