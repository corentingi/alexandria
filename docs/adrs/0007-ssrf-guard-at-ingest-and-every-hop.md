# ADR-0007 — SSRF guard at ingest time and on every hop

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

The design applies the guard to every outbound request. A first version checked only in
the worker, so ingesting `http://192.168.1.1/` created a resource that then failed —
acceptance criterion 8 says it must be rejected.

## Decision

`Library.ingest` and `attach_url` resolve the host and reject private targets with HTTP 400
before anything is written. Downloads check again on every redirect hop; git clones check
the clone host; the GitHub API host is checked too. `localhost`, `*.local`, `*.internal`
are refused by name; IPv4-mapped IPv6 is unwrapped. `security.allow_private_hosts` opts in.

## Why

Immediate feedback, no junk resources, and defence in depth for redirects.

## Cost

- DNS is resolved twice (at ingest and at fetch).
- A time-of-check/time-of-use gap remains: httpx resolves the name again when connecting,
  so a DNS-rebinding host could answer differently. Closing it needs pinning the resolved
  IP in the transport; deferred (LAN-only service, see design §11).
- The test suite swaps the resolver (`security.set_resolver`) and blocks sockets.
