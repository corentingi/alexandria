# ADR-0016 — Deleting an attachment needs the admin scope

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
