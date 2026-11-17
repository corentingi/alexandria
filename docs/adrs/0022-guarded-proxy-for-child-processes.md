# ADR-0022 — Child processes reach the network through a guarded proxy

- **Status:** Accepted (extends ADR-0019)
- **Date:** 2026-11-17

## Context

The SSRF guard checks every address and connects to the address it checked
(ADR-0019). It lives in the HTTP client of this process. yt-dlp is a separate process
that opens its own connections, to dozens of hosts for one video, and follows whatever
the pages and APIs it reads tell it to. Without a guard, a crafted page could make it
request `http://192.168.1.1/` or the cloud metadata address.

## Decision

`proxy.GuardedProxy` is an HTTP proxy started on 127.0.0.1 for one job, with a random
password. yt-dlp gets it as `--proxy`, which covers every request it makes (checked: with
a proxy that does not answer, it fails on its very first request).

- `CONNECT host:port` (HTTPS) and plain `http://` requests both resolve the host with the
  same code as `GuardedBackend`, refuse non-public addresses, and connect to the checked
  address. TLS stays end to end between yt-dlp and the site.
- A plain HTTP request is forwarded once, with `Connection: close`, so a kept-alive
  connection cannot carry a second request to another host.
- It counts the bytes received; past the limit it cuts the connections and refuses new
  ones. This backs up yt-dlp's own `--max-filesize`, which does not apply to every format.
- A refused address makes the fetch fail permanently with the reason.

## Cost

- Every byte of a video passes through Python once more: about one CPU core per
  ~100 MB/s, far above what a home connection gives.
- A program that ignores its proxy setting would not be guarded. yt-dlp honours it;
  ffmpeg is never given a URL.
- The proxy speaks HTTP/1.1 proxying only, which is what yt-dlp uses.
