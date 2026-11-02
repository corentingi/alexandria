# ADR-0003 — API tokens live in `tokens.yaml`, not in the database

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

The design stores token hashes "in the database", but also says the database can be
deleted and rebuilt losing only the job queue. Both cannot be true: a rebuilt database
would lock every agent and the human out.

## Decision

Token SHA-256 hashes, names, scopes and creation dates are stored in
`<data>/tokens.yaml` (mode 0600, written atomically). The file is re-read when its mtime
changes, so `alexandria token create|revoke` takes effect on a running server without a
restart. Comparison is constant-time over every entry.

## Why

Keeps principle 1 intact: the DB stays pure derived data plus the job queue.

## Cost

- One more file to back up (the `backup` command copies it).
- No per-token "last used" tracking (it would mean a write on every request).
