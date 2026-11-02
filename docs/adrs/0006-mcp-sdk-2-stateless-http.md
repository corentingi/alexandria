# ADR-0006 — MCP: SDK 2.x, stateless streamable HTTP, auth in front and per tool

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

The installed `mcp` SDK is 2.x, where `FastMCP` became `MCPServer`. It offers OAuth-based
auth, which the design rules out for v1 (static bearer tokens).

## Decision

- `MCPServer.streamable_http_app(stateless_http=True, json_response=True)` served at `/mcp`
  by a small ASGI dispatcher (`app.Root`) in front of FastAPI. FastAPI's lifespan runs the
  MCP session manager.
- `McpAuth` rejects requests without a valid token with 401 before the MCP transport.
  Each tool reads the `Authorization` header from its request context and checks scope.
- DNS-rebinding Host checks are disabled: the server is reached by LAN IP or hostname, and
  bearer tokens (not cookies) protect it, so a rebinding page cannot authenticate.
- Every tool output is a compact dict; fetched text is under `untrusted_content` /
  `untrusted_snippet`, and the server instructions say it is data, never instructions.

## Why

Stateless mode needs no session affinity and survives restarts; JSON responses are simpler
for clients and tests.

## Cost

No server-initiated notifications (not needed in v1). Tied to SDK 2.x APIs.
