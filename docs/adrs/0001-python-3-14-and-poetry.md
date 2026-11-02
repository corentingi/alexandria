# ADR-0001 — Python 3.14, Poetry, strict typing

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

The design allows Python 3.13+, and prefers 3.14 for the stdlib `uuid.uuid7()`. Python 3.14
is installed on the development machine and `python:3.14-slim` images exist.

## Decision

Target Python 3.14 only (`requires-python = ">=3.14,<3.15"`), managed with Poetry, `src/`
layout, `mypy --strict` and ruff. The Docker image is `python:3.14-slim`, not 3.13.

## Why

- `uuid.uuid7()` from the stdlib removes the `uuid6` dependency.
- A single supported version keeps the matrix trivial for a PoC.

## Cost

- Hosts that only have 3.13 cannot run it outside Docker.
- Some libraries lag on a new minor; none of ours did at the time of writing.
