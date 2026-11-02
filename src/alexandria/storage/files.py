"""On-disk layout: the source of truth (design §5, §6).

Every path is built from an id we generated (validated with ``is_id``) or a sanitized
attachment name checked with ``safe_join``. Nothing here trusts user input as a path.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
from collections.abc import Iterator
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import frontmatter
import yaml

from alexandria.ids import is_id
from alexandria.models import Artifact, Resource, iso, utcnow
from alexandria.security import safe_join

RESOURCE_FILE = "resource.md"
SNAPSHOT_FORMAT = "%Y%m%dT%H%M%SZ"

# Frontmatter key order, so files diff and read nicely.
_KEY_ORDER = [
    "id", "slug", "kind", "title", "title_source", "source_url", "canonical_url", "why",
    "tags", "scope", "project", "status", "fetch_status", "fetch_error", "captured_at",
    "updated_at", "created_by", "meta", "current_snapshot", "snapshots", "attachments",
    "source_file", "generated",
]  # fmt: skip


class InvalidResourceFile(ValueError):
    pass


class FileStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.resources_dir = data_dir / "resources"
        self.tmp_dir = data_dir / "tmp"

    def ensure_layout(self) -> None:
        self.resources_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def wipe_tmp(self) -> None:
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # -- resources ---------------------------------------------------------------

    def resource_dir(self, resource_id: str) -> Path:
        if not is_id(resource_id):
            raise ValueError(f"not a resource id: {resource_id!r}")
        return self.resources_dir / resource_id

    def resource_ids(self) -> Iterator[str]:
        if not self.resources_dir.exists():
            return
        for entry in sorted(self.resources_dir.iterdir()):
            if entry.is_dir() and is_id(entry.name):
                yield entry.name

    def exists(self, resource_id: str) -> bool:
        return (self.resource_dir(resource_id) / RESOURCE_FILE).is_file()

    def read(self, resource_id: str) -> Resource:
        path = self.resource_dir(resource_id) / RESOURCE_FILE
        return parse_resource(path.read_text(encoding="utf-8"), source=str(path))

    def write(self, resource: Resource) -> None:
        """Atomic: write ``resource.md.tmp`` then ``os.replace``."""
        directory = self.resource_dir(resource.id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / RESOURCE_FILE
        tmp = directory / (RESOURCE_FILE + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(render_resource(resource))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)

    def delete(self, resource_id: str) -> None:
        directory = self.resource_dir(resource_id)
        if directory.exists():
            shutil.rmtree(directory)

    # -- snapshots ---------------------------------------------------------------

    def new_staging_dir(self, job_id: str) -> Path:
        path = self.tmp_dir / job_id
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return path

    def new_snapshot_id(self, resource_id: str, now: datetime | None = None) -> str:
        """Timestamp id; bumped by a second if that folder already exists."""
        stamp = (now or utcnow()).timestamp()
        snapshots = self.resource_dir(resource_id) / "snapshots"
        while True:
            candidate = datetime.fromtimestamp(stamp, tz=utcnow().tzinfo).strftime(SNAPSHOT_FORMAT)
            if not (snapshots / candidate).exists():
                return candidate
            stamp += 1

    def install_snapshot(self, staging: Path, resource_id: str, snapshot_id: str) -> Path:
        """Move a finished staging dir into place. Atomic: same filesystem."""
        snapshots = self.resource_dir(resource_id) / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        target = snapshots / snapshot_id
        os.rename(staging, target)
        return target

    def snapshot_dir(self, resource_id: str, snapshot_id: str) -> Path:
        return safe_join(self.resource_dir(resource_id) / "snapshots", snapshot_id)

    def read_artifact_text(self, resource: Resource, role: str) -> str | None:
        artifact = resource.artifact(role)
        if artifact is None or resource.current_snapshot is None:
            return None
        path = safe_join(self.snapshot_dir(resource.id, resource.current_snapshot), artifact.path)
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None

    # -- attachments -------------------------------------------------------------

    def attachments_dir(self, resource_id: str) -> Path:
        path = self.resource_dir(resource_id) / "attachments"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def source_dir(self, resource_id: str) -> Path:
        path = self.resource_dir(resource_id) / "source"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def file_path(self, resource_id: str, relative: str) -> Path:
        """Resolve a download path under ``snapshots/``, ``attachments/`` or ``source/``."""
        root = self.resource_dir(resource_id)
        path = safe_join(root, relative)
        top = path.relative_to(root.resolve()).parts[:1]
        if top not in (("snapshots",), ("attachments",), ("source",)) or not path.is_file():
            raise FileNotFoundError(relative)
        return path


def describe_file(path: Path, role: str, base: Path | None = None, **extra: Any) -> Artifact:
    """Hash and describe a file as an ``Artifact``."""
    return Artifact(
        role=role,
        path=str(path.relative_to(base)) if base else path.name,
        mime=guess_mime(path.name),
        size=path.stat().st_size,
        sha256=sha256_file(path),
        **extra,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def guess_mime(name: str) -> str:
    if name.endswith(".md"):
        return "text/markdown"
    if name.endswith(".tar.gz"):
        return "application/gzip"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


# -- resource.md serialization -----------------------------------------------------


class _Dumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _list_representer(dumper: yaml.SafeDumper, data: list[Any]) -> yaml.SequenceNode:
    flow = all(isinstance(v, (str, int, float, bool)) or v is None for v in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(str, _str_representer)
_Dumper.add_representer(list, _list_representer)


def render_resource(resource: Resource) -> str:
    data = resource.model_dump(mode="python")
    ordered = {key: _plain(data.pop(key)) for key in _KEY_ORDER if key in data}
    ordered.update({k: _plain(v) for k, v in data.items()})
    header = yaml.dump(
        ordered, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    body = resource.notes.rstrip("\n")
    return f"---\n{header}---\n{body}\n" if body else f"---\n{header}---\n"


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value


def parse_resource(text: str, source: str = "<string>") -> Resource:
    try:
        post = frontmatter.loads(text)
    except yaml.YAMLError as exc:
        raise InvalidResourceFile(f"{source}: invalid YAML frontmatter: {exc}") from exc
    try:
        return Resource.model_validate({**post.metadata, "notes": post.content})
    except ValueError as exc:
        raise InvalidResourceFile(f"{source}: {exc}") from exc
