"""Library export and import as plain tar files, split into parts of ~2 GB (ADR-0015).

Every part is a complete, independent tar: a resource folder is never split across two
parts, so parts can be imported one at a time, in any order, and a lost part only loses
the resources it holds. A resource bigger than the part size gets a part of its own.

Layout of a part::

    alexandria-export.json          manifest: format, version, export id, part number
    resources/<id>/resource.md      exactly the on-disk layout of the data dir
    resources/<id>/snapshots/...

Only ``resources/`` is exported. The index is rebuilt from it; tokens, the session secret
and ``config.yaml`` belong to an installation, not to a library.

These are blocking helpers; ``Library`` calls them in a thread, one resource at a time,
holding that resource's lock.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any

from alexandria import __version__
from alexandria.ids import is_id
from alexandria.models import iso, utcnow

FORMAT = "alexandria-library"
VERSION = 1
MANIFEST = "alexandria-export.json"
STATUS_FILE = "export.json"
EXPORT_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")
_BLOCK = 512


class ArchiveError(ValueError):
    """The file is not an Alexandria export, or is damaged."""


@dataclass
class Part:
    name: str
    size: int = 0
    resources: int = 0


@dataclass
class ExportInfo:
    id: str
    status: str = "running"  # running | done | failed
    created_at: str = field(default_factory=lambda: iso(utcnow()))
    finished_at: str | None = None
    resources: int = 0
    size: int = 0
    part_mb: int = 2000
    parts: list[Part] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def load(cls, directory: Path) -> ExportInfo | None:
        try:
            data = json.loads((directory / STATUS_FILE).read_text(encoding="utf-8"))
            data["parts"] = [Part(**p) for p in data.get("parts", [])]
            return cls(**data)
        except OSError, ValueError, TypeError:
            return None

    def save(self, directory: Path) -> None:
        tmp = directory / (STATUS_FILE + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, directory / STATUS_FILE)


def _resource_files(resource_dir: Path) -> list[tuple[Path, str]]:
    """Regular files of one resource, as (path, archive name). Temp files are skipped."""
    out: list[tuple[Path, str]] = []
    base = resource_dir.parent.parent  # the data dir
    for root, dirs, files in os.walk(resource_dir, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            path = Path(root) / name
            if name.endswith(".tmp") or path.is_symlink() or not path.is_file():
                continue
            out.append((path, path.relative_to(base).as_posix()))
    return out


def _tar_size(files: list[tuple[Path, str]]) -> int:
    """Bytes these files take in a tar: a header block each, data padded to 512."""
    total = 0
    for path, _ in files:
        size = path.stat().st_size
        total += _BLOCK + (size + _BLOCK - 1) // _BLOCK * _BLOCK
    return total


class ExportWriter:
    """Writes resources into ``<prefix>-001.tar``, ``-002.tar``… under ``directory``."""

    def __init__(self, directory: Path, info: ExportInfo, part_bytes: int) -> None:
        self.directory = directory
        self.info = info
        self.part_bytes = part_bytes
        self._tar: tarfile.TarFile | None = None
        self._written = 0

    def add_resource(self, resource_dir: Path) -> None:
        files = _resource_files(resource_dir)
        if not files:
            return
        size = _tar_size(files)
        if self._tar is None or (self._written + size > self.part_bytes and self._has_resources):
            self._next_part()
        assert self._tar is not None
        for path, arcname in files:
            self._tar.add(path, arcname=arcname, recursive=False)
        self._written += size
        part = self.info.parts[-1]
        part.resources += 1
        self.info.resources += 1

    @property
    def _has_resources(self) -> bool:
        return bool(self.info.parts) and self.info.parts[-1].resources > 0

    def _next_part(self) -> None:
        self._close_part()
        number = len(self.info.parts) + 1
        name = f"alexandria-{self.info.id}-{number:03d}.tar"
        self.info.parts.append(Part(name=name))
        self._tar = tarfile.open(  # noqa: SIM115 - kept open across resources
            self.directory / name, "w", format=tarfile.PAX_FORMAT
        )
        manifest = json.dumps(
            {
                "format": FORMAT,
                "version": VERSION,
                "export_id": self.info.id,
                "part": number,
                "created_at": self.info.created_at,
                "app_version": __version__,
            },
            indent=2,
        ).encode()
        member = tarfile.TarInfo(MANIFEST)
        member.size = len(manifest)
        member.mtime = int(utcnow().timestamp())
        self._tar.addfile(member, io.BytesIO(manifest))
        self._written = _BLOCK + (len(manifest) + _BLOCK - 1) // _BLOCK * _BLOCK

    def _close_part(self) -> None:
        if self._tar is None:
            return
        self._tar.close()
        part = self.info.parts[-1]
        part.size = (self.directory / part.name).stat().st_size
        self._tar = None

    def close(self) -> None:
        if self._tar is None:  # an empty library still gets one (empty) part
            self._next_part()
        self._close_part()
        self.info.size = sum(p.size for p in self.info.parts)

    def abort(self) -> None:
        if self._tar is not None:
            self._tar.close()
            self._tar = None


# ---------------------------------------------------------------- import


@dataclass
class LibraryImportReport:
    file: str
    export_id: str | None = None
    part: int | None = None
    imported: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    finished_at: str | None = None


def unpack(archive: Path, staging: Path) -> tuple[dict[str, Any], list[str], list[str]]:
    """Extract ``resources/<id>/…`` from an export into ``staging``.

    Returns (manifest, resource ids, problems). Only regular files and directories under
    ``resources/<id>/`` are written; anything else (links, devices, ``..``, absolute
    paths, unknown top-level names) is skipped and reported.
    """
    try:
        tar = tarfile.open(archive, "r:*")  # noqa: SIM115 - closed by the with below
    except (tarfile.TarError, OSError) as exc:
        raise ArchiveError(f"not a tar file: {exc}") from exc
    problems: list[str] = []
    ids: list[str] = []
    manifest: dict[str, Any] | None = None
    with tar:
        try:
            for member in tar:
                if member.name == MANIFEST and member.isfile():
                    manifest = _read_manifest(tar.extractfile(member))
                    continue
                parts = PurePosixPath(member.name).parts
                if (
                    len(parts) < 2
                    or parts[0] != "resources"
                    or not is_id(parts[1])
                    or any(p in ("", ".", "..") for p in parts)
                    or member.name.startswith("/")
                ):
                    problems.append(f"skipped unexpected entry {member.name!r}")
                    continue
                if member.isdir():
                    continue
                if not member.isfile():
                    problems.append(f"skipped non-regular entry {member.name!r}")
                    continue
                if manifest is None:
                    raise ArchiveError(f"{MANIFEST} must come first: not an Alexandria export")
                target = staging.joinpath(*parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                assert source is not None
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, 1 << 20)
                if parts[1] not in ids:
                    ids.append(parts[1])
        except tarfile.TarError as exc:
            raise ArchiveError(f"damaged archive: {exc}") from exc
    if manifest is None:
        raise ArchiveError(f"no {MANIFEST}: not an Alexandria export")
    return manifest, ids, problems


def _read_manifest(fh: IO[bytes] | None) -> dict[str, Any]:
    if fh is None:
        raise ArchiveError("unreadable manifest")
    try:
        data = json.loads(fh.read(64_000))
    except ValueError as exc:
        raise ArchiveError(f"invalid manifest: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ArchiveError("not an Alexandria export")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise ArchiveError(f"export format version {data.get('version')} is not supported")
    return data
