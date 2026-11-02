"""URL canonicalization and kind detection (design §8.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alexandria.models import Kind

TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_GITHUB_RE = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$")
# GitHub paths that look like /<owner>/<repo> but are not repositories.
_GITHUB_RESERVED = {
    "orgs", "users", "settings", "marketplace", "topics", "explore", "sponsors",
    "features", "about", "login", "join", "notifications", "apps", "collections",
    "search", "trending", "pricing", "enterprise", "organizations", "site",
}  # fmt: skip


class InvalidURL(ValueError):
    pass


@dataclass(frozen=True)
class Detected:
    kind: Kind
    canonical_url: str
    forge: str | None = None
    owner: str | None = None
    repo: str | None = None
    # URL to hand to `git clone`.
    clone_url: str | None = None


def canonicalize(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise InvalidURL(f"only http and https URLs are supported, got {scheme or 'none'!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidURL("URL has no host")
    if parts.username or parts.password:
        raise InvalidURL("URLs with credentials are not supported")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidURL(str(exc)) from exc
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{port}"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    )
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def detect(url: str, kind: Kind | None = None) -> Detected:
    canonical = canonicalize(url)
    parts = urlsplit(canonical)
    if parts.hostname in ("github.com", "www.github.com") and kind in (None, Kind.GIT_REPO):
        match = _GITHUB_RE.match(parts.path)
        if match and match.group(1).lower() not in _GITHUB_RESERVED:
            owner, repo = match.group(1).lower(), match.group(2).lower()
            return Detected(
                kind=Kind.GIT_REPO,
                canonical_url=f"https://github.com/{owner}/{repo}",
                forge="github",
                owner=owner,
                repo=repo,
                clone_url=f"https://github.com/{owner}/{repo}.git",
            )
    if kind == Kind.GIT_REPO or (kind is None and parts.path.endswith(".git")):
        return Detected(kind=Kind.GIT_REPO, canonical_url=canonical, clone_url=canonical)
    if kind == Kind.NOTE:
        raise InvalidURL("a note has no URL; use create_note")
    return Detected(kind=Kind.ARTICLE, canonical_url=canonical)
