# ADR-0023 — The command line works next to the running server

- **Status:** Accepted
- **Date:** 2026-11-18

## Context

The CLI now covers every common action (`edit`, `refetch`, attachments, snapshots, jobs),
so it is how a shell inside the container manages the library. It is a separate process
that opens the same data directory as the server. The locks that kept read-modify-writes
of `resource.md` apart were `asyncio.Lock`s, so they could not see each other across
processes. A CLI `edit` could then drop a snapshot the server was installing, or the
server could drop the edit (a human field). A CLI `refetch` could also queue a second
fetch of a resource the UI had just queued.

## Decision

- The CLI talks to the files through `Library`, as every interface does, not to the
  server over HTTP. It needs no token and works while the server is down.
- `Library.lock(resource_id)` and `Library.ingest_lock()` take an `flock` on
  `<data>/locks/<key>.lock`, as well as the in-process asyncio lock. Waiting polls
  (`LOCK_NB`), so a cancelled task never leaves a thread blocked on a lock. Lock files are
  never deleted: a newcomer could otherwise lock a fresh file while the old one is held.
- Jobs queued by the CLI wait in the shared SQLite queue. The server's worker picks them
  up at its next poll (`worker.poll_interval_s`, 1 s). `--now` runs them in the CLI
  process instead.
- Bulk commands (`refetch` with filters) refuse empty filter values. An unset shell
  variable must not widen "these" to "everything".

## Cost

- One small file per resource ever locked, under `<data>/locks/`.
- `flock` needs a local filesystem (NFS/SMB locks are unreliable). That was already a
  requirement for SQLite WAL.
- A restart of the server wipes `tmp/` and re-queues running jobs, including work a CLI
  process is doing at that moment (an `upload` or `attachment add` being staged, an
  `add --now` fetch). Run such commands again after a restart.
