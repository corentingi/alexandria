# Command line

Everything you do in the web UI, you can do from a shell: in scripts, over SSH on a NAS,
or inside the container. `alexandria --help` and `alexandria <command> --help` list every
option.

```sh
docker compose exec alexandria alexandria --help    # Docker Compose
docker exec alexandria alexandria --help            # docker run --name alexandria
ALEXANDRIA_DATA_DIR=$PWD/dev-data poetry run alexandria --help   # from source
```

## How it works

- Commands open the data directory directly (`ALEXANDRIA_DATA_DIR`, `/data` in the image),
  through the same `Library` as the server. No token is needed: whoever can run a shell
  there already has the files.
- Work that takes time (fetching, extracting, importing links) is **queued**. The running
  server's worker picks it up within a second. `jobs list` follows it. `add --now` and
  `upload --now` do the work in the command itself.
- The CLI and the server can run at the same time: each resource is locked across
  processes while it changes (ADR-0023). What the CLI saves is recorded as created by
  `cli`.
- A **ref** is a resource's id or slug, as `list` and `show` print them.
- Read commands take `--json` and print the REST API's shape, for scripts.
- Exit code 0 means done, 1 means refused (the message says why), 2 means a wrong option.

## Save

```sh
alexandria add https://example.com/post --why "explains WAL well" --tag sqlite --tag db
alexandria add https://github.com/owner/repo --project research
alexandria add https://youtu.be/VIDEO --keep video --max-height 1080
alexandria add https://peertube.example/w/abc --video     # any site yt-dlp supports
alexandria add https://example.com/post --now             # fetch now, wait for it
alexandria upload paper.pdf notes.docx --tag papers
alexandria import bookmarks.html --tag imported --keep audio
alexandria note "Meeting 12 March" --body-file notes.md   # or --body-file - for stdin
```

- Without `--why`, a resource waits in the inbox.
- `--keep` is `transcript` (the default), `audio` or `video`, for videos only.
  `--max-height` (360, 480, 720, 1080, 1440, 2160) needs `--keep video`. It caps the
  resolution, which is the frame's shorter side, so a vertical 1080x1920 video is 1080p.
- `import` reads plain text with links, a Markdown list or a browser bookmarks export.
  Links already saved are skipped. Its `--keep` applies to the videos among the links.

## Find and read

```sh
alexandria list                                  # newest first, archived left out
alexandria list --kind media --site youtube.com --limit 50
alexandria list --status inbox
alexandria list --fetch-status failed
alexandria search "write-ahead log" --tag sqlite
alexandria lookup https://example.com/post       # saved already? (canonical URL)
alexandria show my-post                          # details, files, where they are on disk
alexandria show my-post --json
alexandria content my-post                       # the text: article, transcript, document
alexandria content my-post --chunk 3             # one chunk (see show --json)
alexandria tags
alexandria projects
alexandria stats                                 # resources per status, jobs waiting
```

Filters: `--kind` (article, git_repo, note, document, media), `--status` (inbox, kept,
archived), `--tag` (repeatable: all must match), `--project`, `--fetch-status` (pending,
fetching, ready, failed), `--site` (a host and its subdomains).

## Change

```sh
alexandria edit my-post --why "the best explanation of WAL"   # inbox → kept
alexandria edit my-post --tag wal --untag db --title "WAL, explained"
alexandria edit my-post --notes-file review.md                # or --notes "text"
alexandria edit my-post --project research                    # --project '' : global
alexandria edit my-post --status archived
alexandria edit my-video --keep video --max-height 720        # applies at the next fetch
alexandria delete my-post                                     # asks; --yes to skip
```

`--tag` adds and `--untag` removes. The other tags stay. Options left out are left as
they are.

## Fetch again

```sh
alexandria refetch my-post other-post
alexandria refetch --site youtube.com          # every match; asks first, --yes to skip
alexandria refetch --fetch-status failed --yes
alexandria refetch --all
```

- Name resources, or select them with filters or `--all`, not both.
- Notes are left out, since they have no source. Archived resources are left out unless
  you add `--status archived`.
- An empty filter value is refused, so `refetch --tag "$UNSET"` never means "everything".
- A video's file is downloaded again only when its options changed or a higher resolution
  is offered. Videos of one site download one at a time.

## Snapshots and attachments

Each fetch adds a snapshot and keeps the previous ones.

```sh
alexandria snapshot list my-video
alexandria snapshot prune my-video                    # keep only the current snapshot
alexandria snapshot prune --site youtube.com --yes    # the same, for every match
alexandria snapshot delete my-video 20261117T141301Z  # one snapshot (never the last)

alexandria attachment add my-post slides.pdf          # a file
alexandria attachment add my-post https://example.com/paper.pdf   # downloaded
alexandria attachment extract my-post slides.pdf      # extract its text again
alexandria attachment delete my-post slides.pdf
```

`prune` reports the space it actually frees. A video file shared with the current
snapshot (a re-fetch that reused it) stays.

## Jobs

```sh
alexandria jobs list                     # newest first
alexandria jobs list --status failed
alexandria jobs show JOB_ID              # with its payload: an import's report
alexandria jobs retry JOB_ID
alexandria jobs retry --failed           # every failed job
```

## Administration

```sh
alexandria version                       # 0.2.0, or 0.2.0-3-g1a2b3c4 between releases
alexandria token create claude --scope write    # prints the secret once
alexandria token list
alexandria token scope claude read
alexandria token revoke claude
alexandria reindex                       # rebuild the index from the files
alexandria backup /backups/alexandria-2026-11-18
alexandria library export --out /exports       # tar parts of about 2 GB
alexandria library exports
alexandria library delete-export 20261118T101500Z
alexandria library import part-001.tar part-002.tar
alexandria serve --port 8080             # what the container runs
```

`token scope` and `token revoke` may remove the last admin token: the command line is the
way back in.
