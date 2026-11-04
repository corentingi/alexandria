# ADR-0019 — The SSRF guard pins the connection to the checked address

- **Status:** Accepted (amends ADR-0007)
- **Date:** 2026-11-04

## Context

ADR-0007 checks every URL, at ingest and on each redirect hop, by resolving its host and
refusing non-public addresses. httpx and `git` then resolved the host **again** to
connect. A DNS server answering a public address to the check and `127.0.0.1` or
`169.254.169.254` to the connection (DNS rebinding, TTL 0) reached internal services.

## Decision

The address that was checked is the address connected to.

- **HTTP:** `http_client()` installs `security.GuardedBackend` as httpcore's network
  backend. Every TCP connection resolves its host, checks every address, and connects
  to the first one. TLS still verifies the certificate against the host name, which
  httpcore passes separately. The `check_url` calls before each hop stay, for clear
  error messages.
- **git:** `_guard` returns a `host:port:address` entry that is passed as
  `http.curloptResolve`, so curl inside git never resolves the host itself (git 2.37+).
- Allow-listed private hosts (`security.allow_private_hosts`) are connected to by name.
- The HTTP client ignores proxy environment variables (`trust_env=False`): a proxy would
  connect on our behalf, out of the guard's reach.

## Cost

- httpx has no public hook for the network backend: `http_client()` sets
  `transport._pool._network_backend`. A test pins this shape, so an httpx upgrade that
  breaks it fails the suite instead of silently dropping the guard.
- Fetching through an HTTP proxy is no longer possible.
- The image needs git 2.37 or later (Debian's is 2.47).
