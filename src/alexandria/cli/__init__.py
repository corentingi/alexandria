"""Command line (design §10.4): every common action, for scripts and for a shell inside the
container. Commands work on the data directory directly, through ``Library`` like every
interface; the running server's worker picks up the jobs they queue from the shared SQLite
queue. Read commands take ``--json`` for the REST API's shape.

A feature added to the UI, REST or MCP gets its command here too (AGENTS.md).
"""

from alexandria.cli import resources  # noqa: F401, I001  (registers its commands first)
from alexandria.cli import admin  # noqa: F401
from alexandria.cli._common import app

__all__ = ["app"]
