# ADR-0010 — UI: cookie holding the token, CSRF tokens, strict CSP, no remote images

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

The UI shows fetched pages (untrusted Markdown) and must not become a way to run their code
or leak reads. It is server-rendered (Jinja2 + vendored htmx 2.0.4), with no build step.

## Decision

- Login stores the token in a signed (itsdangerous), HttpOnly, SameSite=Lax cookie. Every
  request re-verifies the token, so revoking it ends its sessions.
- Every POST carries a CSRF token (HMAC of the cookie), as a form field or the
  `X-CSRF-Token` header that `app.js` adds to htmx requests.
- CSP `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:` — no
  inline script or style anywhere in the templates; htmx `allowEval` off.
- Markdown is rendered with raw HTML disabled; links get `rel=noopener noreferrer nofollow`;
  images become links instead of loading from third parties.
- Artifacts and attachments are served as downloads (`Content-Disposition: attachment`,
  `CSP: sandbox`, `nosniff`), never rendered inline.
- Tokens cannot be created in the UI; the secret is only ever printed by the CLI.

## Cost

No inline previews of archived HTML or images; reading content means reading the
extracted Markdown. Session secret persisted in `<data>/session_secret` if not configured.
