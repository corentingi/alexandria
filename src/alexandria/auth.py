"""Scoped bearer tokens (design §11), stored as SHA-256 hashes in ``<data>/tokens.yaml``.

Not in the database: the database must stay rebuildable from files (ADR-0003).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

import yaml

from alexandria.models import iso, utcnow

TOKEN_PREFIX = "alx_"  # noqa: S105 - a prefix, not a secret
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.@-]{0,39}$")


class TokenError(ValueError):
    pass


class ScopeLevel(IntEnum):
    READ = 1
    WRITE = 2
    ADMIN = 3

    @classmethod
    def parse(cls, value: str) -> ScopeLevel:
        try:
            return cls[value.upper()]
        except KeyError as exc:
            raise ValueError(f"unknown scope {value!r}; use read, write or admin") from exc

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Principal:
    name: str
    scope: ScopeLevel

    def can(self, needed: ScopeLevel) -> bool:
        return self.scope >= needed


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._tokens: list[dict[str, Any]] = []

    def _load(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                self._tokens, self._mtime = [], None
                return []
            if mtime != self._mtime:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                self._tokens = list(data.get("tokens", []))
                self._mtime = mtime
            return self._tokens

    def _save(self, tokens: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump({"tokens": tokens}, sort_keys=False), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, self.path)
        with self._lock:
            self._mtime = None

    def list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in t.items() if k != "sha256"} for t in self._load()]

    def create(self, name: str, scope: ScopeLevel) -> str:
        name = name.strip()
        if not name:
            raise TokenError("a token needs a name")
        if not _NAME.match(name):
            raise TokenError(
                "a token name has up to 40 letters, digits, spaces or . _ @ - characters"
            )
        tokens = list(self._load())
        if any(t["name"] == name for t in tokens):
            raise TokenError(f"a token named {name!r} already exists")
        secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
        tokens.append(
            {
                "name": name,
                "scope": scope.label,
                "sha256": _hash(secret),
                "created_at": iso(utcnow()),
            }
        )
        self._save(tokens)
        return secret

    def revoke(self, name: str, keep_an_admin: bool = False) -> bool:
        """Remove a token. With ``keep_an_admin``, refuse to remove the last admin one."""
        tokens = self._load()
        kept = [t for t in tokens if t["name"] != name]
        if len(kept) == len(tokens):
            return False
        if keep_an_admin and not _has_admin(kept):
            raise TokenError("this is the last admin token; create another admin token first")
        self._save(kept)
        return True

    def set_scope(self, name: str, scope: ScopeLevel, keep_an_admin: bool = True) -> None:
        tokens = [dict(t) for t in self._load()]
        token = next((t for t in tokens if t["name"] == name), None)
        if token is None:
            raise TokenError(f"no token named {name!r}")
        token["scope"] = scope.label
        if keep_an_admin and not _has_admin(tokens):
            raise TokenError("this is the last admin token; create another admin token first")
        self._save(tokens)

    def verify(self, secret: str | None) -> Principal | None:
        if not secret:
            return None
        digest = _hash(secret.strip())
        found: Principal | None = None
        for token in self._load():
            # Compare every entry, in constant time, so timing reveals nothing.
            if hmac.compare_digest(digest, str(token.get("sha256", ""))):
                found = Principal(token["name"], ScopeLevel.parse(token["scope"]))
        return found

    def is_empty(self) -> bool:
        return not self._load()


def _has_admin(tokens: list[dict[str, Any]]) -> bool:
    return any(t.get("scope") == ScopeLevel.ADMIN.label for t in tokens)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None
