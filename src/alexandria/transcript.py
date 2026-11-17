"""Captions (WebVTT) to a readable, searchable Markdown transcript (ADR-0021).

Automatic captions on YouTube roll: each cue repeats the previous line and adds a few
words, with inline timing tags. Lines already seen in the last few cues are dropped, so
the transcript reads once. Cues are grouped into paragraphs of about a minute, each
starting with its timestamp, and under the video's chapters when it has some.
"""

from __future__ import annotations

import html
import re
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

_TIMING = re.compile(r"^((?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\s+-->\s+")
_TAG = re.compile(r"<[^>]*>")
_PARAGRAPH_S = 60.0


@dataclass
class Cue:
    start: float
    text: str


def parse_vtt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    # Cues are separated by empty lines; a line holding a space is still part of the cue
    # (YouTube starts every automatic cue with one).
    text = text.replace("﻿", "").replace("\r\n", "\n")
    for block in re.split(r"\n\n+", text):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            match = _TIMING.match(line.strip())
            if match:
                body = " \n".join(lines[i + 1 :])
                body = html.unescape(_TAG.sub("", body))
                cues.append(Cue(_seconds(match.group(1)), body))
                break
    return cues


def _seconds(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return total


def dedupe(cues: list[Cue]) -> list[Cue]:
    """Drop lines repeated from recent cues (rolling automatic captions)."""
    recent: deque[str] = deque(maxlen=4)
    out: list[Cue] = []
    for cue in cues:
        new = []
        for line in cue.text.splitlines():
            line = " ".join(line.split())
            if line and line not in recent:
                new.append(line)
                recent.append(line)
        if new:
            out.append(Cue(cue.start, " ".join(new)))
    return out


def timestamp(seconds: float) -> str:
    s = int(seconds)
    hours, rest = divmod(s, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def link_at(url: str, seconds: float) -> str | None:
    """The page URL at this moment, for sites that support it."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    s = int(seconds)
    if host == "youtube.com" and dict(parse_qsl(parts.query)).get("v"):
        return f"{url}&t={s}s"
    if host == "vimeo.com":
        return f"{url}#t={s}s"
    return None


def transcript_markdown(
    cues: list[Cue], url: str | None, chapters: list[dict[str, Any]] | None = None
) -> str:
    cues = dedupe(cues)
    marks = sorted(
        (float(c.get("start_time") or 0), str(c.get("title") or "").strip())
        for c in chapters or []
        if c.get("title")
    )
    blocks: list[str] = []
    paragraph: list[str] = []
    start = 0.0

    def flush() -> None:
        if paragraph:
            stamp = timestamp(start)
            link = link_at(url, start) if url else None
            prefix = f"[{stamp}]({link})" if link else f"**{stamp}**"
            blocks.append(f"{prefix} {' '.join(paragraph)}")
            paragraph.clear()

    for cue in cues:
        heading = None
        while marks and marks[0][0] <= cue.start:
            heading = marks.pop(0)[1]
        if heading is not None or (paragraph and cue.start - start >= _PARAGRAPH_S):
            flush()
        if heading is not None:
            blocks.append(f"### {_escape_line(heading)}")
        if not paragraph:
            start = cue.start
        paragraph.append(cue.text)
    flush()
    return "\n\n".join(blocks)


def _escape_line(line: str) -> str:
    """Untrusted plain text that must not turn into a heading, quote or list."""
    stripped = line.lstrip()
    if stripped[:1] in ("#", ">", "-", "*", "+", "=") or re.match(r"\d+[.)]\s", stripped):
        return "\\" + stripped
    return stripped


def plain_text_markdown(text: str) -> str:
    """A description: plain text, lines kept as written."""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    return "\n\n".join(
        "  \n".join(_escape_line(line) for line in p.splitlines() if line.strip())
        for p in paragraphs
        if p.strip()
    )
