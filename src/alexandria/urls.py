"""URL canonicalization and kind detection (design §8.1)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alexandria.models import Kind

# Only parameters that never select content. "ref" is not one: it is a branch or tag on
# GitLab and GitHub, a version on many sites. Merging two different pages loses one of
# them; keeping a tracking parameter only risks a duplicate, which the content hash flags.
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid",
    "_hsenc", "_hsmi", "mkt_tok", "ref_src", "ref_url",
}  # fmt: skip
# Left in the address bar by bot checks (Reddit's JS challenge): never part of the page.
CHALLENGE_PARAMS = {"js_challenge", "jsc_token", "jsc_orig_r", "solution"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_GITHUB_RE = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$")
# GitHub paths that look like /<owner>/<repo> but are not repositories.
_GITHUB_RESERVED = {
    "orgs", "users", "settings", "marketplace", "topics", "explore", "sponsors",
    "features", "about", "login", "join", "notifications", "apps", "collections",
    "search", "trending", "pricing", "enterprise", "organizations", "site",
}  # fmt: skip


# Video and audio sites recognized without being asked (ADR-0021). yt-dlp supports many
# more; any of them can be saved as media by choosing the kind explicitly.
_YOUTUBE_HOSTS = {
    "youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "youtube-nocookie.com",
}  # fmt: skip
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_PATH = re.compile(r"^/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})$")
_VIMEO = re.compile(r"^/(?:video/)?(\d+)$")
_DAILYMOTION = re.compile(r"^/video/([A-Za-z0-9]+)")
_TWITCH_VOD = re.compile(r"^/videos/(\d+)$")
_SOUNDCLOUD_TRACK = re.compile(r"^/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$")
_SOUNDCLOUD_RESERVED = {"discover", "stream", "upload", "you", "search", "charts", "pages"}


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
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    drop = set(TRACKING_PARAMS)
    if any(k.lower() == "js_challenge" for k, _ in pairs):
        drop |= CHALLENGE_PARAMS
    # Sorted by name only: repeated keys keep their order (?a=2&a=1 is not ?a=1&a=2).
    query = sorted(
        ((k, v) for k, v in pairs if not k.lower().startswith("utm_") and k.lower() not in drop),
        key=lambda kv: kv[0],
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
    if kind in (None, Kind.MEDIA):
        media_url = _media_url(canonical)
        if media_url:
            return Detected(kind=Kind.MEDIA, canonical_url=media_url)
        if kind == Kind.MEDIA:
            return Detected(kind=Kind.MEDIA, canonical_url=canonical)
    return Detected(kind=Kind.ARTICLE, canonical_url=canonical)


def _media_url(canonical: str) -> str | None:
    """The one address of a single video on a known site, or ``None``.

    Every way of linking the same video (short links, embeds, Shorts, a timestamp or a
    playlist around it) gives the same URL, so it is saved once. Playlists, channels and
    other pages are not single videos and stay articles.
    """
    parts = urlsplit(canonical)
    host = (parts.hostname or "").removeprefix("www.")
    path = parts.path
    if host in _YOUTUBE_HOSTS:
        video_id = None
        if host == "youtu.be":
            video_id = path.strip("/")
        elif path == "/watch":
            video_id = dict(parse_qsl(parts.query)).get("v")
        elif match := _YOUTUBE_PATH.match(path):
            video_id = match.group(1)
        if video_id and _YOUTUBE_ID.match(video_id):
            return f"https://www.youtube.com/watch?v={video_id}"
        return None
    if host in ("vimeo.com", "player.vimeo.com") and (match := _VIMEO.match(path)):
        return f"https://vimeo.com/{match.group(1)}"
    if host == "dailymotion.com" and (match := _DAILYMOTION.match(path)):
        return f"https://www.dailymotion.com/video/{match.group(1).split('_', 1)[0]}"
    if host == "dai.ly" and path.strip("/").isalnum():
        return f"https://www.dailymotion.com/video/{path.strip('/')}"
    if host in ("twitch.tv", "m.twitch.tv") and (match := _TWITCH_VOD.match(path)):
        return f"https://www.twitch.tv/videos/{match.group(1)}"
    if host in ("soundcloud.com", "m.soundcloud.com") and (match := _SOUNDCLOUD_TRACK.match(path)):
        user, track = match.groups()
        if user not in _SOUNDCLOUD_RESERVED and track not in ("sets", "likes", "tracks"):
            return f"https://soundcloud.com/{user}/{track}"
    return None
