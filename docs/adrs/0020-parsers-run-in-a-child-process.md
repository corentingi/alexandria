# ADR-0020 — Untrusted files are parsed in a child process

- **Status:** Accepted (amends ADR-0012)
- **Date:** 2026-11-04

## Context

pypdf, lxml and trafilatura parse files anyone with a write token can send: uploads,
attachments, fetched pages. These parsers have a history of inputs that make them spin
or allocate without end. They ran in `asyncio.to_thread`, and a thread cannot be
stopped: one crafted PDF held a worker slot at 100% CPU until the server restarted, and
the job's retries took the other slot.

## Decision

`isolate.run_isolated(task, args, limits)` runs the parser in `python -m
alexandria.isolate`, a child process that can only run the tasks it lists (`extract`,
`article`).

- It is killed after `extract.timeout_s` (300 s), or when the job is cancelled.
- Its address space is capped at `extract.max_memory_mb` (2 GB) with `RLIMIT_AS`. This is
  enforced on Linux (the Docker image) but not on macOS.
- A timeout, an out-of-memory error or a crash is an extraction failure: the resource
  keeps its original file and shows `extraction: failed` with the reason. The job is not
  retried, since the same file would fail the same way.
- Arguments and results travel as JSON over stdin and stdout; the files stay on disk.
- `extract.isolate: false` runs the same code in a thread, for debugging.

## Cost

- Each extraction starts a Python process and imports its parser, which adds about half
  a second. That is acceptable for background jobs.
- The parse result (up to 10 MB of Markdown) is serialized once more.
