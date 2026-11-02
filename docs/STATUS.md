# Status

## Acceptance criteria (design §18)

| # | Criterion | How it is verified |
| - | --------- | ------------------ |
| 1 | `docker compose up` on an empty volume gives a working UI | By hand: image built, healthy, bootstrap token logged, UI served (2026-11-02) |
| 2 | Article → id at once, then `ready` with page/content/fetch, searchable | `test_library.py::TestArticle::test_ingest_fetch_index`, `test_interfaces.py::TestRest::test_ingest_flow`; by hand against a real blog |
| 3 | Small GitHub repo → tar.gz; restore gives every branch, tag, commit; big → shallow | `TestGit::test_full_and_shallow`, `TestGit::test_github_metadata`; by hand: octocat/Spoon-Knife restored with all branches |
| 4 | No `why` → inbox; adding one → kept | `TestInboxAndUpdate::test_inbox_then_kept`, `TestUi::test_pages_and_add_flow` |
| 5 | Known URL → existing resource, `duplicate: true` | `TestArticle::test_duplicate_returns_existing`, `TestRest::test_ingest_flow` |
| 6 | Delete DB + reindex → identical search results | `TestReindex::test_delete_db_and_reindex_is_identical` |
| 7 | read can't ingest; write can't delete | `TestRest::test_scopes`, `TestMcp::test_tools` |
| 8 | `192.168.1.1` / `localhost` rejected | `TestSSRF`, `TestArticle::test_ssrf_rejected`, `TestRest::test_ssrf`, redirect case too |
| 9 | Re-fetch → second snapshot, first kept | `TestArticle::test_refetch_keeps_old_snapshot` |
| 10 | Suite runs without network | `conftest.py` blocks sockets and DNS for every test |

## Known limits

- DNS-rebinding TOCTOU between the SSRF check and the connection (ADR-0007).
- Trafilatura sometimes turns lines starting with `#` inside unfenced code into headings.
- Git clone timeout falls back to shallow on *any* non-"not found" failure, not only size.
- No incremental `git fetch` on re-fetch: each snapshot is a full new clone.
- `stars`, `topics`… are refreshed only on re-fetch.

## Next steps

See design §15 (LLM enrichment, embeddings) and the ideas in the roadmap.
