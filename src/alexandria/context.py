"""Process-wide objects shared by every interface."""

from __future__ import annotations

from dataclasses import dataclass

from alexandria.auth import TokenStore
from alexandria.config import Settings
from alexandria.jobs import Worker
from alexandria.library import Library


@dataclass
class AppContext:
    settings: Settings
    library: Library
    worker: Worker
    tokens: TokenStore

    def enqueued(self) -> None:
        self.worker.wake()
