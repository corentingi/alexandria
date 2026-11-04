# Architecture decision records

One file per decision someone might re-litigate. Each records the context, the choice, why,
and what it costs. A record is never edited to change its decision: write a new one and
mark the old one superseded.

| ADR | Title | Status |
| --- | ----- | ------ |
| [0001](0001-python-3-14-and-poetry.md) | Python 3.14, Poetry, strict typing | Accepted |
| [0002](0002-files-are-the-source-of-truth.md) | Files are the source of truth; SQLite is a rebuildable index | Accepted |
| [0003](0003-tokens-live-in-a-file.md) | API tokens live in `tokens.yaml`, not in the database | Accepted |
| [0004](0004-bootstrap-admin-token.md) | First start creates an admin token and logs it once | Accepted |
| [0005](0005-synchronous-index-calls.md) | Index and file calls run inline on the event loop | Accepted |
| [0006](0006-mcp-sdk-2-stateless-http.md) | MCP: SDK 2.x, stateless streamable HTTP, auth in front and per tool | Accepted |
| [0007](0007-ssrf-guard-at-ingest-and-every-hop.md) | SSRF guard at ingest time and on every hop | Accepted |
| [0008](0008-title-source-and-slug-after-first-fetch.md) | `title_source`; article slugs settle after the first fetch | Accepted |
| [0009](0009-note-body-is-content.md) | A note's body is its content, indexed once | Accepted |
| [0010](0010-ui-security-model.md) | UI: cookie holding the token, CSRF, strict CSP, no remote images | Accepted |
| [0011](0011-failed-refetch-keeps-resource-ready.md) | A failed re-fetch keeps the resource usable | Accepted |
| [0012](0012-documents-and-text-extraction.md) | Documents: original in `source/`, text extracted into snapshots | Accepted |
| [0013](0013-bulk-link-import.md) | Bulk import of links from text or a bookmarks export | Accepted |
| [0014](0014-hardening-after-review.md) | Hardening after the first review | Accepted |
| [0015](0015-library-export-and-import.md) | Library export and import as independent ~2 GB tar parts | Accepted |
| [0016](0016-deleting-attachments-needs-admin.md) | Deleting an attachment or a snapshot needs the admin scope | Accepted |
| [0017](0017-blocked-sites-and-attachment-text.md) | Sites that block fetches; attachment text standing in | Accepted |
| [0018](0018-manage-tokens-in-the-ui.md) | Tokens are managed in the UI too (amends 0010) | Accepted |
| [0019](0019-ssrf-guard-pins-the-connection.md) | The SSRF guard pins the connection to the checked address (amends 0007) | Accepted |
