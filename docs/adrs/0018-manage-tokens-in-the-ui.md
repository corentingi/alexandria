# ADR-0018 — Tokens are managed in the UI too

- **Status:** Accepted (amends ADR-0010)
- **Date:** 2026-11-03

## Context

ADR-0010 kept token creation in the CLI so a secret would never appear in a browser. On
a NAS, a shell inside the container is awkward, and every new agent needs a token.

## Decision

Admins create, re-scope and revoke tokens on the Settings page; the CLI keeps all of it
plus a new `alexandria token scope`.

- A new secret appears **once**, in the body of the POST response (`Cache-Control:
  no-store`), never in a URL, redirect, log or later page. Only its hash is stored.
- Names: 1–40 characters, letters, digits, space and `. _ @ -` (they appear in URLs and
  in `created_by`).
- The UI refuses to revoke or re-scope **the token you are signed in with**, and to
  remove or downgrade **the last admin token**: nobody can lock themselves out by a click.
- The CLI may do both (`keep_an_admin=False`): it is the recovery path, together with
  `token create`, when every admin token is lost.
- Revocation takes effect on the next request (the file is re-read on change); browser
  sessions holding that token end too, since a session carries the token itself.

## Cost

- A secret is now shown in a browser: anyone looking at the screen, or a screenshot,
  gets it. Mitigated by showing it once and naming who it is for.
- Still no per-token "last used" date (ADR-0003).
