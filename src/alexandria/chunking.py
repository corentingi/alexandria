"""Markdown chunking (design §7).

Split on ATX headings, then split long sections on paragraph boundaries into chunks of at
most ``MAX_CHARS``. Deterministic: the same text always yields the same chunks, so v2 can
key embeddings on them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHARS = 2000
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^(```|~~~)")


@dataclass(frozen=True)
class Chunk:
    ord: int
    heading_path: str
    text: str
    role: str | None = None  # overrides the upsert's source role (attachment text)


def chunk_markdown(text: str, max_chars: int = MAX_CHARS) -> list[Chunk]:
    sections = _sections(text.replace("\r\n", "\n"))
    chunks: list[Chunk] = []
    for heading_path, body in sections:
        for piece in _split(body, max_chars):
            chunks.append(Chunk(ord=len(chunks), heading_path=heading_path, text=piece))
    return chunks


def _sections(text: str) -> list[tuple[str, str]]:
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line.strip()):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match:
            level, title = len(match.group(1)), match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sections.append((" > ".join(t for _, t in stack), [line]))
        else:
            sections[-1][1].append(line)
    return [(path, "\n".join(lines).strip()) for path, lines in sections if "".join(lines).strip()]


def _split(body: str, max_chars: int) -> list[str]:
    if len(body) <= max_chars:
        return [body]
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for part in _hard_split(paragraph, max_chars):
            if current and len(current) + 2 + len(part) > max_chars:
                pieces.append(current)
                current = part
            else:
                current = f"{current}\n\n{part}" if current else part
    if current:
        pieces.append(current)
    return pieces


def _hard_split(paragraph: str, max_chars: int) -> list[str]:
    """A single paragraph longer than the cap is cut on whitespace."""
    if len(paragraph) <= max_chars:
        return [paragraph]
    parts: list[str] = []
    rest = paragraph
    while len(rest) > max_chars:
        cut = rest.rfind(" ", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts
