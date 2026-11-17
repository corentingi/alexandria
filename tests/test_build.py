from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alexandria import __version__, build


class TestBuild:
    @pytest.mark.parametrize(
        ("describe", "shown"),
        [
            ("v0.1.0\n", "0.1.0"),
            ("v0.1.0-8-g2482ce1", "0.1.0-8-g2482ce1"),
            ("v0.1.0-8-g2482ce1-dirty", "0.1.0-8-g2482ce1-dirty"),
            ("2482ce1", f"{__version__}+2482ce1"),
            ("", __version__),
        ],
    )
    def test_version_from_describe(self, describe: str, shown: str) -> None:
        assert build.version_from(describe) == shown

    def test_image_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "_build.txt"
        f.write_text("v0.2.0-3-gabc1234\n2026-11-17\n")
        monkeypatch.setattr(build, "BUILD_FILE", f)
        build.build.cache_clear()
        try:
            assert build.build() == build.Build("0.2.0-3-gabc1234", "2026-11-17")
            f.write_text("\n2026-11-17\n")  # built without the git context
            build.build.cache_clear()
            assert build.build() == build.Build(__version__, "2026-11-17")
        finally:
            build.build.cache_clear()

    def test_healthz(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        f = tmp_path / "_build.txt"
        f.write_text("v0.2.0-3-gabc1234\n2026-11-17\n")
        monkeypatch.setattr(build, "BUILD_FILE", f)
        build.build.cache_clear()
        try:
            assert client.get("/healthz").json()["version"] == "0.2.0-3-gabc1234"
        finally:
            build.build.cache_clear()
