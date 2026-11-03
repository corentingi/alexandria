# ADR-0015 — Library export and import as independent tar parts

- **Status:** Accepted
- **Date:** 2026-11-03

## Context

The owner asked for a way to export and import a complete library from the settings,
split into files of about 2 GB. `alexandria backup` already exists, but it copies a
directory on the server's disk and is reachable only from a shell.

## Decision

- **Content:** only `resources/`, with exactly its on-disk layout. The index is rebuilt
  from the files on import (ADR-0002). Tokens, the session secret and `config.yaml` belong
  to an installation, so they stay out: an export can be shared without handing over
  credentials.
- **Format:** plain, uncompressed `.tar` (PAX). Most of the volume is already compressed
  (git `tar.gz` archives, PDF, DOCX, EPUB). Without compression, sizes are known before
  writing, so parts split precisely, and anyone can read a part with `tar` alone.
- **Splitting:** parts are filled up to `export.part_mb` (default 2000 MB, under the
  2 GiB that some upload paths and tools limit files to). A resource folder is **never
  split**: every part is a complete tar that starts with `alexandria-export.json` (format,
  version, export id, part number). Parts can be imported one at a time, in any order,
  and a lost part only loses its own resources. A resource bigger than the part size gets
  a part of its own, which can be larger than `part_mb`.
- **Consistency:** each resource is copied while holding its lock, so it is never caught
  half-written. The export is not one point-in-time snapshot of the whole library;
  resources changed while the export runs may be in either state.
- **Import never overwrites.** A resource is skipped if its id already exists, or if its
  canonical URL is already used by another resource (for example the same PDF saved in
  both libraries). A clashing slug gets a suffix. A resource exported while its fetch was
  pending is queued for fetching again, because its job stayed in the other database.
  Archive entries outside `resources/<id>/`, and links, devices or `..` paths, are skipped
  and reported. Files are extracted by hand, never with `extractall`.
- **Running:** both are jobs (`export`, `import_library`), visible in Activity. Exports
  are written to `<data>/exports/<id>/` with an `export.json` status file. Uploaded parts
  wait in `<data>/imports/` and are deleted once imported; a `*.report.json` file records
  what each import did. Admin scope only. CLI: `alexandria library export [--out DIR]`
  and `alexandria library import <parts…>`.

## Cost

- An export temporarily needs free space equal to the size of the library, and stays on
  disk until deleted from the settings.
- A browser upload is written twice: once by the multipart parser (into `<data>/tmp`, set
  through `TMPDIR` in the image) and once into `imports/`. For very large libraries, the
  CLI avoids both the upload and the extra copy.
- Uncompressed parts are larger for text-heavy libraries (articles, notes).
- Without merge semantics, a resource edited in both libraries keeps the local version.
