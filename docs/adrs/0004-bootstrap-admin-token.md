# ADR-0004 — First start creates an admin token and logs it once

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

Acceptance criterion 1: `docker compose up` on an empty volume gives a working UI. The UI
needs a token, and tokens are created with the CLI, which a NAS user may not know how to
run inside a container.

## Decision

If `tokens.yaml` has no tokens at startup, create an `admin` token and print it in the log
inside a banner, once. Its hash is stored like any other token.

## Why

Zero-step first run (`docker logs alexandria` shows the token), and no default password.

## Cost

The secret appears in container logs. Mitigation: it can be revoked and replaced with
`docker exec alexandria alexandria token ...` at any time.
