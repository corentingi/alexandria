# ADR-0005 — Index and file calls run inline on the event loop

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

The design asks for a single writer connection guarded by a lock, and reads on separate
connections. The app is async (FastAPI, MCP, the worker).

## Decision

`Index` uses stdlib `sqlite3` synchronously. Writes go through one connection behind a
`threading.RLock` (safe for the event loop, threads and the CLI); each read opens its own
short-lived connection. These calls, and local file I/O, run directly in async code rather
than in a thread pool. CPU-heavy work (trafilatura, tarball creation) runs in
`asyncio.to_thread`. Ruff's ASYNC109/ASYNC240 are disabled for this reason.

## Why

Queries on a personal library take microseconds to milliseconds; a thread hop costs more
than it saves and makes transactions harder to reason about.

## Cost

A pathological query (huge library, broad FTS match) briefly blocks the loop. If that is
ever observed, wrap `Index` reads in `to_thread`; the interface does not change.
