"""Library export and import (ADR-0015)."""

from __future__ import annotations

import io
import json
import re
import tarfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from alexandria.config import Settings, load_settings
from alexandria.jobs import Worker
from alexandria.library import InvalidInput, Library
from alexandria.models import FetchStatus
from alexandria.transfer import MANIFEST
from tests.fixtures import make_pdf
from tests.test_interfaces import TestUi as _InterfaceUi  # aliased: not collected twice
from tests.test_interfaces import auth


async def stream(data: bytes) -> AsyncIterator[bytes]:
    yield data


def other_library(tmp_path: Path, name: str = "other") -> Library:
    return Library(load_settings(data_dir=tmp_path / name))


def tar_names(path: Path) -> list[str]:
    with tarfile.open(path) as tar:
        return tar.getnames()


def make_tar(path: Path, entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    with tarfile.open(path, "w") as tar:
        for info, data in entries:
            if data is not None:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    return path


def manifest_entry() -> tuple[tarfile.TarInfo, bytes]:
    data = json.dumps({"format": "alexandria-library", "version": 1, "export_id": "x"}).encode()
    return tarfile.TarInfo(MANIFEST), data


async def seed(library: Library, worker: Worker) -> list[str]:
    a = await library.create_note("Heron notes", "The heron waits by the estuary.", why="birds")
    b = await library.create_note("Owl notes", "Owls hunt at night.")
    await library.attach_upload(a.id, "map.txt", stream(b"x" * 3000))
    doc = await library.ingest_file("survey.pdf", stream(make_pdf(["Egret census 2026"])))
    await worker.drain()
    return [a.id, b.id, doc.resource.id]


class TestExport:
    async def test_round_trip_in_independent_parts(
        self, library: Library, worker: Worker, tmp_path: Path
    ) -> None:
        ids = await seed(library, worker)
        library.settings.export.part_mb = 0  # forces one part per resource
        info = await library.export_library("20261103T120000Z")
        assert info.status == "done" and info.resources == 3
        assert len(info.parts) == 3 and all(p.resources == 1 for p in info.parts)
        directory = library.settings.exports_dir / info.id
        for part in info.parts:
            names = tar_names(directory / part.name)
            assert names[0] == MANIFEST
            assert len({n.split("/")[1] for n in names[1:]}) == 1  # one whole resource

        target = other_library(tmp_path)
        try:
            imported: list[str] = []
            for part in reversed(info.parts):  # any order
                report = await target.import_library(directory / part.name)
                assert report.errors == [] and report.export_id == info.id
                imported += report.imported
            assert sorted(imported) == sorted(ids)
            hits = target.search("heron estuary")
            assert [row["id"] for _, row in hits] == [ids[0]]
            assert [row["id"] for _, row in target.search("egret")] == [ids[2]]
            copy = target.get(ids[0])
            assert copy.why == "birds" and copy.attachments[0].path == "map.txt"
            original = library.files.resource_dir(ids[0]) / "attachments" / "map.txt"
            copied = target.files.resource_dir(ids[0]) / "attachments" / "map.txt"
            assert copied.read_bytes() == original.read_bytes()

            again = await target.import_library(directory / info.parts[0].name)
            assert again.imported == [] and len(again.existing) == 1
        finally:
            target.close()

    async def test_parts_grouped_by_size(self, library: Library, worker: Worker) -> None:
        await seed(library, worker)
        info = await library.export_library("20261103T120001Z")
        assert len(info.parts) == 1 and info.parts[0].resources == 3

    async def test_empty_library(self, library: Library) -> None:
        info = await library.export_library("20261103T120002Z")
        assert info.resources == 0 and len(info.parts) == 1

    async def test_one_export_at_a_time(self, library: Library) -> None:
        library.start_export()
        with pytest.raises(InvalidInput, match="already"):
            library.start_export()


class TestImport:
    async def test_duplicate_link_and_slug_clash(
        self, library: Library, worker: Worker, tmp_path: Path
    ) -> None:
        await seed(library, worker)
        info = await library.export_library("20261103T120003Z")
        part = library.settings.exports_dir / info.id / info.parts[0].name
        target = other_library(tmp_path)
        try:
            # Same PDF (same urn:sha256 link) and a note with the same title, other ids.
            await target.ingest_file("copy.pdf", stream(make_pdf(["Egret census 2026"])))
            await target.create_note("Heron notes", "mine")
            report = await target.import_library(part)
            assert len(report.duplicates) == 1 and len(report.imported) == 2
            slugs = sorted(r.slug for r in (target.get(i) for i in report.imported))
            assert slugs == ["heron-notes-2", "owl-notes"]
        finally:
            target.close()

    async def test_pending_fetch_is_queued_again(
        self, library: Library, worker: Worker, tmp_path: Path
    ) -> None:
        result = await library.ingest("https://blog.example.com/a")  # not fetched yet
        info = await library.export_library("20261103T120004Z")
        target = other_library(tmp_path)
        try:
            report = await target.import_library(
                library.settings.exports_dir / info.id / info.parts[0].name
            )
            assert report.imported == [result.resource.id]
            assert target.get(result.resource.id).fetch_status == FetchStatus.PENDING
            assert target.index.pending_jobs(result.resource.id, "fetch") == 1
        finally:
            target.close()

    async def test_hostile_archive(self, library: Library, tmp_path: Path) -> None:
        rid = "019a0000-0000-7000-8000-000000000000"
        link = tarfile.TarInfo(f"resources/{rid}/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        path = make_tar(
            tmp_path / "evil.tar",
            [
                manifest_entry(),
                (tarfile.TarInfo("resources/../../escape.txt"), b"x"),
                (tarfile.TarInfo("/abs.txt"), b"x"),
                (tarfile.TarInfo("tokens.yaml"), b"x"),
                (link, None),
            ],
        )
        report = await library.import_library(path)
        assert report.imported == [] and len(report.errors) == 4
        assert not (tmp_path / "escape.txt").exists()
        assert not any(library.settings.resources_dir.iterdir())

    async def test_not_an_export(self, library: Library, tmp_path: Path) -> None:
        plain = make_tar(tmp_path / "plain.tar", [(tarfile.TarInfo("a.txt"), b"x")])
        with pytest.raises(InvalidInput, match="not an Alexandria export"):
            await library.import_library(plain)
        junk = tmp_path / "junk.tar"
        junk.write_bytes(b"not a tar")
        with pytest.raises(InvalidInput, match="not a tar"):
            await library.import_library(junk)


class TestTransferUi:
    def test_export_download_import(
        self, client: TestClient, tokens: dict[str, str], settings: Settings
    ) -> None:
        w = auth(tokens["write"])
        nid = client.post("/api/v1/notes", json={"title": "Kept", "body": "lamp"}, headers=w)
        assert nid.status_code == 201
        csrf = _InterfaceUi().login(client, tokens["admin"])
        res = client.post("/settings/export", data={"csrf": csrf}, follow_redirects=False)
        assert res.status_code == 303 and "Export+started" in res.headers["location"]

        link = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and link is None:
            page = client.get("/settings/transfers").text
            match = re.search(r'href="(/settings/exports/[^"]+\.tar)"', page)
            link = match.group(1) if match else None
            time.sleep(0.05)
        assert link, "export did not finish"
        part = client.get(link)
        assert part.status_code == 200 and part.content[:512].count(b"alexandria-export") == 1

        res = client.post(
            "/settings/import",
            data={"csrf": csrf},
            files=[("file", ("part.tar", part.content, "application/x-tar"))],
            follow_redirects=False,
        )
        assert res.status_code == 303 and "Importing" in res.headers["location"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            page = client.get("/settings/transfers").text
            if "already here" in page:
                break
            time.sleep(0.05)
        assert "1 already here" in page and "0 imported" in page
        assert not list(settings.imports_dir.glob("*.tar"))  # upload removed after import

    def test_admin_only(self, client: TestClient, tokens: dict[str, str]) -> None:
        csrf = _InterfaceUi().login(client, tokens["write"])
        assert client.post("/settings/export", data={"csrf": csrf}).status_code == 403
        assert client.get("/settings/exports/20261103T120000Z/x.tar").status_code == 403
        assert "Export library" not in client.get("/settings").text
