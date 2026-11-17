"""Which build is running: the release, or the commit it was built from.

``__version__`` only changes at a release, so every build in between looked the same.
The Docker image records ``git describe`` and the build date in ``_build.txt``; a source
checkout asks git directly. A release built on its tag still reads ``0.1.0``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from alexandria import __version__

_HERE = Path(__file__).parent
BUILD_FILE = _HERE / "_build.txt"


@dataclass(frozen=True)
class Build:
    version: str  # "0.1.0", or "0.1.0-8-g2482ce1" between releases
    date: str | None = None  # the image's build date (UTC), when known


def version_from(describe: str) -> str:
    """``git describe --tags --always`` output to a version to show."""
    describe = describe.strip()
    if not describe:
        return __version__
    if describe.startswith("v") and describe[1:2].isdigit():
        return describe[1:]
    # No tag reachable (a shallow clone): only the commit is known.
    return f"{__version__}+{describe}"


def _from_git() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(_HERE), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return out.stdout


@cache
def build() -> Build:
    try:
        lines = BUILD_FILE.read_text().splitlines()
    except OSError:
        return Build(version_from(_from_git() or ""))
    describe = lines[0] if lines else ""
    date = lines[1].strip() if len(lines) > 1 else ""
    return Build(version_from(describe), date or None)
