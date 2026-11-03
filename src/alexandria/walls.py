"""Sites that need a login, block automated fetches, or sit behind a paywall (ADR-0017).

Two deterministic checks, no network:

- ``site_warning(url)``: a known site, warned about *before* saving (UI, REST, MCP).
- ``detect_block(html, url)``: a fetched page that is a bot check or login wall rather
  than content, so the fetch fails with a clear message instead of an empty snapshot.

The workaround is the same everywhere: save the link anyway, then attach a PDF of the
page (browser: Print → Save as PDF). Its text is extracted and searched (ADR-0017).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

LOGIN = "login"  # content is behind a login wall
BOT = "bot"  # automated fetches are blocked or challenged
PAYWALL = "paywall"  # articles are often truncated for non-subscribers
SCRIPT = "script"  # pages are rendered by JavaScript; the HTML is nearly empty

TIP = "Save it anyway, then attach a PDF of the page (Print → Save as PDF): its text is searched."
AFTER_TIP = "Attach a PDF of the page (Print → Save as PDF): its text will be used instead."

_REASONS = {
    LOGIN: "{site} shows most content only to signed-in users",
    BOT: "{site} blocks automated fetches",
    PAYWALL: "{site} articles are often behind a paywall",
    SCRIPT: "{site} pages are built by JavaScript, which Alexandria does not run",
}


@dataclass(frozen=True)
class SiteWarning:
    site: str
    kind: str
    message: str


# Registrable domain -> (display name, kind). Subdomains match too.
KNOWN_SITES: dict[str, tuple[str, str]] = {
    "reddit.com": ("Reddit", BOT),
    "redd.it": ("Reddit", BOT),
    "linkedin.com": ("LinkedIn", LOGIN),
    "lnkd.in": ("LinkedIn", LOGIN),
    "facebook.com": ("Facebook", LOGIN),
    "fb.com": ("Facebook", LOGIN),
    "instagram.com": ("Instagram", LOGIN),
    "threads.net": ("Threads", LOGIN),
    "threads.com": ("Threads", LOGIN),
    "x.com": ("X", LOGIN),
    "twitter.com": ("X", LOGIN),
    "tiktok.com": ("TikTok", SCRIPT),
    "quora.com": ("Quora", LOGIN),
    "pinterest.com": ("Pinterest", LOGIN),
    "glassdoor.com": ("Glassdoor", LOGIN),
    "discord.com": ("Discord", LOGIN),
    "slack.com": ("Slack", LOGIN),
    "teams.microsoft.com": ("Microsoft Teams", LOGIN),
    "docs.google.com": ("Google Docs", LOGIN),
    "drive.google.com": ("Google Drive", LOGIN),
    "notion.so": ("Notion", SCRIPT),
    "notion.site": ("Notion", SCRIPT),
    "figma.com": ("Figma", SCRIPT),
    "medium.com": ("Medium", PAYWALL),
    "nytimes.com": ("The New York Times", PAYWALL),
    "wsj.com": ("The Wall Street Journal", PAYWALL),
    "ft.com": ("Financial Times", PAYWALL),
    "bloomberg.com": ("Bloomberg", PAYWALL),
    "economist.com": ("The Economist", PAYWALL),
    "washingtonpost.com": ("The Washington Post", PAYWALL),
    "theatlantic.com": ("The Atlantic", PAYWALL),
    "newyorker.com": ("The New Yorker", PAYWALL),
    "lemonde.fr": ("Le Monde", PAYWALL),
    "lefigaro.fr": ("Le Figaro", PAYWALL),
    "mediapart.fr": ("Mediapart", PAYWALL),
    "lesechos.fr": ("Les Echos", PAYWALL),
}


def _site(url: str) -> tuple[str, str] | None:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    while host:
        if host in KNOWN_SITES:
            return KNOWN_SITES[host]
        if "." not in host:
            return None
        host = host.split(".", 1)[1]
    return None


def site_warning(url: str) -> SiteWarning | None:
    found = _site(url)
    if found is None:
        return None
    name, kind = found
    return SiteWarning(name, kind, f"{_REASONS[kind].format(site=name)}. {TIP}")


# Markers of interstitial pages, matched on the first part of a short HTML page.
_BLOCK_MARKERS: list[tuple[bytes, str]] = [
    (b"js_challenge", "a bot check"),
    (b"jsc_token", "a bot check"),
    (b"cf-chl", "a Cloudflare challenge"),
    (b"challenge-platform", "a Cloudflare challenge"),
    (b"captcha-delivery.com", "a DataDome captcha"),
    (b"px-captcha", "a PerimeterX captcha"),
    (b"_incapsula_resource", "an Imperva challenge"),
    (b"aws-waf-token", "an AWS WAF challenge"),
    (b"authwall", "a login wall"),
]
_BLOCK_TITLES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^just a moment", re.I), "a Cloudflare challenge"),
    (re.compile(r"^attention required", re.I), "a Cloudflare challenge"),
    (re.compile(r"^access denied", re.I), "an access-denied page"),
    (re.compile(r"^(are you a robot|robot check|verify you are human)", re.I), "a captcha"),
    (re.compile(r"^(sign in|log in|login)\b", re.I), "a login page"),
]
_TITLE = re.compile(rb"<title[^>]*>([^<]{0,200})", re.I)
_SCAN_BYTES = 200_000


def detect_block(html: bytes, url: str, extracted_chars: int) -> str | None:
    """Why this page is an interstitial rather than content, or ``None``.

    Only short extractions are checked: a real article that happens to mention
    "cf-chl" in a script keeps its content.
    """
    if extracted_chars >= 1500:
        return None
    head = html[:_SCAN_BYTES]
    lowered = head.lower()
    what = next((label for marker, label in _BLOCK_MARKERS if marker in lowered), None)
    if what is None:
        match = _TITLE.search(head)
        title = match.group(1).decode("utf-8", "replace").strip() if match else ""
        what = next((label for pattern, label in _BLOCK_TITLES if pattern.search(title)), None)
    if what is None:
        return None
    found = _site(url)
    site = found[0] if found else (urlsplit(url).hostname or "the site")
    return f"{site} answered with {what} instead of the page. {AFTER_TIP}"
