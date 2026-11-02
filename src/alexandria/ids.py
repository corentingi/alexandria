"""Identifiers: UUIDv7 ids (folder names) and human-friendly slugs (never in paths)."""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Callable

_SLUG_MAX = 60
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def new_id() -> str:
    return str(uuid.uuid7())


def is_id(value: str) -> bool:
    """True if ``value`` looks like one of our ids. Used to validate path components."""
    return bool(_UUID_RE.match(value))


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if len(slug) > _SLUG_MAX:
        slug = slug[:_SLUG_MAX].rsplit("-", 1)[0] or slug[:_SLUG_MAX]
    return slug or "untitled"


def unique_slug(base: str, taken: Callable[[str], bool]) -> str:
    """``base``, then ``base-2``, ``base-3``… until ``taken`` says no."""
    base = slugify(base)
    if is_id(base):  # a slug must never be mistaken for an id
        base = f"r-{base}"
    candidate, n = base, 1
    while taken(candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate
