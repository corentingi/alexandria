"""Configuration: environment variables (``ALEXANDRIA_*``) over ``<data_dir>/config.yaml``.

Precedence, highest first: explicit keyword arguments, environment variables,
``config.yaml``, defaults. Nested keys use ``__`` in environment variables, e.g.
``ALEXANDRIA_GITHUB__TOKEN``.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from alexandria import __version__


class HttpSettings(BaseModel):
    host: str = "0.0.0.0"  # noqa: S104 - LAN service, bound on all interfaces by design
    port: int = 8080
    session_secret: str | None = None
    # Public base URL used in the bookmarklet. Derived from the request when unset.
    # An https:// URL also marks the session cookie Secure (TLS ends at a proxy).
    public_url: str | None = None
    # Reverse proxies whose X-Forwarded-* headers are trusted ("*" or comma-separated
    # IPs), e.g. the Synology reverse proxy. Unset: those headers are ignored.
    forwarded_allow_ips: str | None = None


class FetchSettings(BaseModel):
    timeout_s: float = 30  # per connect or read; a trickling server never trips it
    max_redirects: int = 5
    max_bytes: int = 20_000_000
    user_agent: str = f"Alexandria/{__version__}"
    # The whole download must average at least this, or it fails (and is retried):
    # deadline = timeout_s + max_bytes / min_bytes_per_s.
    min_bytes_per_s: int = 50_000

    def deadline_s(self, max_bytes: int) -> float:
        return self.timeout_s + max_bytes / max(1, self.min_bytes_per_s)


class GitSettings(BaseModel):
    full_history_max_mb: int = 500
    shallow_depth: int = 1000
    clone_timeout_s: float = 900
    max_archive_mb: int = 2000


class GithubSettings(BaseModel):
    token: str | None = None
    api_url: str = "https://api.github.com"


class AttachmentSettings(BaseModel):
    max_mb: int = 500


class ExportSettings(BaseModel):
    # Library exports are split into independent tar parts of about this size.
    part_mb: int = 2000
    # Largest export part accepted by an upload in the UI (a single resource bigger than
    # part_mb gets a part of its own, so this is larger).
    max_upload_mb: int = 4096


class WorkerSettings(BaseModel):
    concurrency: int = 2
    max_attempts: int = 3
    poll_interval_s: float = 1.0
    # Backoff before attempt 2, 3, ... (seconds).
    backoff_s: list[float] = Field(default_factory=lambda: [60.0, 300.0, 1800.0])


class SecuritySettings(BaseModel):
    allow_private_hosts: list[str] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ALEXANDRIA_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    data_dir: Path = Path("/data")
    http: HttpSettings = Field(default_factory=HttpSettings)
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    git: GitSettings = Field(default_factory=GitSettings)
    github: GithubSettings = Field(default_factory=GithubSettings)
    attachments: AttachmentSettings = Field(default_factory=AttachmentSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, _YamlSource(settings_cls))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "alexandria.db"

    @property
    def resources_dir(self) -> Path:
        return self.data_dir / "resources"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "tokens.yaml"


class _YamlSource(PydanticBaseSettingsSource):
    """Reads ``config.yaml`` from the data dir named by the environment (or the default)."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        data_dir = Path(os.environ.get("ALEXANDRIA_DATA_DIR", "/data"))
        path = data_dir / "config.yaml"
        if not path.is_file():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{path} must contain a mapping")
        return loaded


def load_settings(**overrides: Any) -> Settings:
    settings = Settings(**overrides)
    if not settings.http.session_secret:
        settings.http.session_secret = _persistent_secret(settings.data_dir)
    return settings


def _persistent_secret(data_dir: Path) -> str:
    """A session secret kept in the volume, so restarts do not log everyone out."""
    path = data_dir / "session_secret"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        data_dir.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(32)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)
        return value
