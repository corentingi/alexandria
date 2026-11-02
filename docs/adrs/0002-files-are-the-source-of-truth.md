# ADR-0002 — Files are the source of truth; SQLite is a rebuildable index

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

Principle 1 of the design. Recorded here because several later choices follow from it.

## Decision

Every write goes to `resources/<id>/resource.md` first (atomic `os.replace`), then to the
index. `reindex` rebuilds every index table from the files, in one transaction, and leaves
`jobs` alone. Snapshots are built in `tmp/<job-id>/` and moved in with `os.rename`.

## Why

- A NAS snapshot or `rsync` of `resources/` is a complete backup.
- A corrupt or deleted database costs nothing but the job queue.
- Acceptance test: delete the DB, reindex, get identical search results (tested).

## Cost

- Anything not derivable from files cannot live in the DB alone (see ADR-0003 for tokens).
- Two writes per change; a crash between them leaves the index stale until the next write or
  `reindex`. Acceptable: the file is the truth and reindex is cheap.
