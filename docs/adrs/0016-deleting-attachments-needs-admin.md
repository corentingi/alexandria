# ADR-0016 — Deleting an attachment or a snapshot needs the admin scope

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

Attachments could be added but not removed. Adding one needs the write scope, which most
agents hold.

## Decision

`Library.delete_attachment(ref, path)` removes the entry from `resource.md` first, then
the file, then updates the index. It is exposed as `DELETE
/api/v1/resources/{ref}/attachments/{path}` and as a trash button on the resource page,
both with the **admin** scope, like deleting a resource. It is not an MCP tool.

## Cost

A write-scoped user cannot undo their own mistaken upload; an admin has to. Accepted:
deletion is the one irreversible operation, and agents with write tokens should not be
able to remove files a human attached.

## Addendum (2026-11-03): snapshots

Each re-fetch of a git repository stores a complete new clone, so old snapshots can take a
lot of space. `Library.delete_snapshot(ref, id)` follows the same rules: admin scope,
`DELETE /api/v1/resources/{ref}/snapshots/{id}` and a trash button per snapshot, not in MCP.

- The **last remaining snapshot cannot be deleted** (re-fetch first, or delete the
  resource): a fetched resource always has content to show.
- Deleting the current snapshot makes the newest remaining one current, and the index is
  rebuilt from it. `meta` keeps the values of the latest fetch.
- Refused while a fetch is queued or running for the resource.
- Snapshot ids must match the timestamp format before any folder is removed, since an
  imported `resource.md` could list `.` (which would be the whole `snapshots/` folder).
