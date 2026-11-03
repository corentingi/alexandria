# ADR-0017 — Sites that block fetches, and attachment text standing in

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

A saved Reddit post archived Reddit's JavaScript bot check (8 KB, title "Reddit") and was
shown as `ready` with `extraction: poor`. The owner attached a PDF of the page, which was
stored but not read. LinkedIn, X, Facebook and paywalled newspapers behave the same way.

## Decision

- **Warn before saving.** `walls.KNOWN_SITES` lists sites that need a login, block bots,
  are paywalled or are rendered by JavaScript (subdomains match). The Add page shows the
  warning as the URL is typed (and for bookmarklet captures); REST and MCP `ingest`
  return it as `warning`. The resource page shows it until text is available.
- **Fail clearly instead of archiving a bot check.** When a fetched HTML page yields under
  1,500 characters and contains a known interstitial marker (Reddit `js_challenge`,
  Cloudflare, DataDome, PerimeterX, Imperva, AWS WAF, LinkedIn `authwall`) or title ("Just
  a moment…", "Access denied", "Sign in…"), the fetch fails permanently with the reason.
  No snapshot is kept. HTTP 401/403/429 from a known site says which site and why.
- **Challenge parameters are not part of the link.** When `js_challenge` is present,
  `js_challenge`, `jsc_token`, `jsc_orig_r` and `solution` are dropped by
  `canonicalize`, so the same post saved twice is a duplicate.
- **Attachment text.** Attaching a PDF, DOCX, EPUB, HTML, Markdown or text file queues an
  `extract_attachment` job, using the document extractor (ADR-0012). The text is written
  to `attachments/.text/<name>.md` and recorded on the attachment (`text_path`,
  `extraction`) in `resource.md`, so a reindex rebuilds it from files. Its chunks are
  indexed with role `attachment` and headings prefixed by the file name.
- **Standing in.** When the fetched content is missing or weak (no snapshot, extraction
  `poor`/`failed`/`unsupported`), `Library.text` returns the first attachment's text
  (role `attachment`): MCP `get_content` and the resource page use it. A machine-derived
  title (from the URL, or from a weak page) is replaced by the attachment's title
  (metadata, else file name). A human title is never touched.
- Getting around bot checks (browser impersonation, solving challenges) is out of scope.

## Cost

- The site list is hand-kept and will drift; a missing site only means no early warning.
- The block markers can misfire on a short page that mentions them; they apply only below
  1,500 extracted characters, and a failed fetch can be retried.
- Attachments of older resources have no text until "Extract text" is pressed.
- The user still has to print the page to PDF; a browser extension that sends the
  rendered page would remove that step (not built).
