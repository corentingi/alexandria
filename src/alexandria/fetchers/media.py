"""Video and audio pages, fetched with yt-dlp (ADR-0021).

Two yt-dlp runs per fetch, in a child process that is killed on timeout, with every
request going through ``GuardedProxy`` (ADR-0022):

1. ``-J``: the video's details, nothing downloaded. Playlists, live streams and videos
   over the duration limit are handled here, and the captions language is chosen.
2. ``--load-info-json``: captions in that one language (asking for every variant gets
   HTTP 429 from YouTube), the thumbnail, and the audio or video file when asked.

The snapshot holds ``content.md`` (title, description, transcript: what search and
agents read), ``info.json`` (the details, without the expiring stream URLs), the captions
file, the thumbnail and the media file. A re-fetch hard-links the media file of the
previous snapshot when the same thing is asked, instead of downloading it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alexandria.config import Settings
from alexandria.fetchers.base import FetchError, PermanentFetchError, SnapshotResult
from alexandria.models import Artifact, MediaChoice, MediaLevel, Resource
from alexandria.proxy import GuardedProxy
from alexandria.storage.files import FileStore, describe_file
from alexandria.transcript import parse_vtt, plain_text_markdown, timestamp, transcript_markdown

log = logging.getLogger(__name__)

# Errors a retry will not fix (lowercase, matched in yt-dlp's last error line).
_PERMANENT = (
    "video unavailable", "private video", "this video is not available",
    "members-only", "join this channel", "confirm your age", "age-restricted",
    "unsupported url", "has been removed", "copyright", "does not exist",
    "http error 404", "http error 410", "not available in your country", "geo restrict",
    "this live event will begin", "premieres in", "requested format is not available",
)  # fmt: skip
_BOT_CHECK = ("not a bot", "sign in to confirm")
# Stream URLs expire within hours and are long: not worth keeping in info.json.
_DROPPED_INFO_KEYS = {
    "formats", "requested_formats", "requested_downloads", "requested_subtitles",
    "automatic_captions", "subtitles", "heatmap", "http_headers", "thumbnails",
    "_format_sort_fields", "url", "manifest_url", "fragments", "fragment_base_url",
    "_version", "filename", "_filename",
}  # fmt: skip
_MB = 1_000_000
# How yt-dlp is started; the test suite swaps in a fake.
YT_DLP = [sys.executable, "-m", "yt_dlp"]


@dataclass(frozen=True)
class MediaTools:
    """Programs found at startup. Without ffmpeg there are no audio or video files (the
    slim image); without a JavaScript runtime YouTube gives fewer formats."""

    ffmpeg: str | None
    js_runtime: str | None  # yt-dlp's --js-runtimes value, e.g. "deno:/usr/local/bin/deno"


@functools.cache
def find_tools() -> MediaTools:
    ffmpeg = shutil.which("ffmpeg")
    ffmpeg = ffmpeg if ffmpeg and shutil.which("ffprobe") else None
    js_runtime = None
    if deno := shutil.which("deno"):
        js_runtime = f"deno:{deno}"
    elif (node := shutil.which("node")) and _node_major(node) >= 22:
        js_runtime = f"node:{node}"
    return MediaTools(ffmpeg, js_runtime)


def _node_major(node: str) -> int:
    try:
        out = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
        return int(out.strip().lstrip("v").split(".", 1)[0])
    except OSError, ValueError, subprocess.SubprocessError:
        return 0


def downloads_enabled(settings: Settings, tools: MediaTools | None = None) -> bool:
    if settings.media.downloads is not None:
        return settings.media.downloads
    return (tools or find_tools()).ffmpeg is not None


def allowed_levels(settings: Settings, tools: MediaTools | None = None) -> list[MediaLevel]:
    if downloads_enabled(settings, tools):
        return list(MediaLevel)
    return [MediaLevel.TRANSCRIPT]


@dataclass
class _Plan:
    keep: MediaLevel
    height: int
    skipped: str | None = None


class MediaFetcher:
    def __init__(
        self,
        settings: Settings,
        tools: MediaTools | None = None,
        command: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self._tools = tools
        self.command = command or list(YT_DLP)
        self.files = FileStore(settings.data_dir)

    @property
    def tools(self) -> MediaTools:
        return self._tools or find_tools()

    def plan(self, resource: Resource) -> _Plan:
        media = self.settings.media
        choice = resource.media or MediaChoice(keep=media.keep)
        plan = _Plan(choice.keep, choice.max_height or media.max_height)
        if plan.keep != MediaLevel.TRANSCRIPT and not downloads_enabled(self.settings, self.tools):
            plan.keep = MediaLevel.TRANSCRIPT
            plan.skipped = "this server saves transcripts only (no ffmpeg)"
        return plan

    async def fetch(self, resource: Resource, staging_dir: Path) -> SnapshotResult:
        url = resource.canonical_url or resource.source_url
        assert url, "a media resource always has a URL"
        plan = self.plan(resource)
        limit = self.settings.media.max_mb * _MB
        async with GuardedProxy(
            self.settings.security.allow_private_hosts, max_bytes=limit + 200 * _MB
        ) as proxy:
            info = await self._info(url, proxy, staging_dir)
            self._check_info(info, plan)
            full_info = staging_dir / ".info-full.json"
            full_info.write_text(json.dumps(info), encoding="utf-8")
            captions_lang, automatic = _pick_captions(info)
            reused = None
            if plan.keep != MediaLevel.TRANSCRIPT:
                reused = self._reuse(resource, plan, staging_dir)
            args = [
                "--load-info-json", str(full_info), "--write-thumbnail",
                "-o", "media.%(ext)s", "-o", "subtitle:captions.%(ext)s",
                "-o", "thumbnail:thumbnail.%(ext)s",
            ]  # fmt: skip
            if captions_lang:
                args += [
                    "--write-auto-subs" if automatic else "--write-subs",
                    "--sub-langs", re.escape(captions_lang), "--sub-format", "vtt/best",
                ]  # fmt: skip
            if plan.keep == MediaLevel.TRANSCRIPT or reused:
                args.append("--skip-download")
            else:
                args += self._format_args(plan)
                args += [
                    "--max-filesize", f"{self.settings.media.max_mb}M",
                    "--print-to-file", "after_move:%(height|)s", "media-height.txt",
                ]  # fmt: skip
            output = "\n".join(await self._run(args, proxy, staging_dir))
        full_info.unlink(missing_ok=True)
        return self._result(url, info, plan, staging_dir, captions_lang, automatic, reused,
                            output)  # fmt: skip

    # -- yt-dlp runs ----------------------------------------------------------------

    async def _info(self, url: str, proxy: GuardedProxy, staging: Path) -> dict[str, Any]:
        out, _ = await self._run(["-J", "--flat-playlist", "--", url], proxy, staging)
        try:
            info = json.loads(out)
        except ValueError:
            raise FetchError("yt-dlp gave no details for this link") from None
        if not isinstance(info, dict):
            raise FetchError("yt-dlp gave no details for this link")
        return info

    def _check_info(self, info: dict[str, Any], plan: _Plan) -> None:
        if info.get("_type") in ("playlist", "multi_video"):
            raise PermanentFetchError(
                "this link is a playlist or a channel, not a single video; save the videos "
                "one by one"
            )
        if info.get("live_status") in ("is_live", "is_upcoming") or info.get("is_live"):
            raise PermanentFetchError(
                "this is a live stream that has not ended; re-fetch it once it has"
            )
        max_s = self.settings.media.max_duration_min * 60
        duration = info.get("duration") or 0
        if plan.keep != MediaLevel.TRANSCRIPT and duration > max_s:
            plan.keep = MediaLevel.TRANSCRIPT
            plan.skipped = f"longer than {self.settings.media.max_duration_min} minutes"

    def _format_args(self, plan: _Plan) -> list[str]:
        """Formats browsers play (H.264 and AAC first); separate video and audio streams
        are merged with ffmpeg into an MP4, from local files only."""
        if plan.keep == MediaLevel.AUDIO:  # kept as served: no conversion, no ffmpeg
            return ["-f", "bestaudio[ext=m4a]/bestaudio"]
        h = plan.height
        if self.tools.ffmpeg:
            return [
                "-f", f"bv*[height<={h}]+ba/b[height<={h}]/wv*+ba/w",
                "-S", "vcodec:h264,acodec:m4a,ext:mp4:m4a",
                "--merge-output-format", "mp4", "--ffmpeg-location", self.tools.ffmpeg,
            ]  # fmt: skip
        return ["-f", f"b[height<={h}]/w"]  # one file with sound: nothing to merge

    def _reuse(self, resource: Resource, plan: _Plan, staging: Path) -> Artifact | None:
        """Hard-link the previous snapshot's media file when it is what is asked again."""
        previous = resource.artifact("media")
        if previous is None or resource.current_snapshot is None:
            return None
        extra = previous.model_extra or {}
        if extra.get("keep") != plan.keep.value:
            return None
        if plan.keep == MediaLevel.VIDEO and extra.get("max_height") != plan.height:
            return None
        try:
            source = self.files.snapshot_dir(resource.id, resource.current_snapshot)
            os.link(source / previous.path, staging / previous.path)
        except (OSError, ValueError) as exc:
            log.info("cannot reuse the media file of %s: %s", resource.id, exc)
            return None
        return previous

    def _base_args(self, proxy: GuardedProxy) -> list[str]:
        args = [
            "--ignore-config", "--no-progress", "--no-mtime", "--no-playlist",
            "--no-write-playlist-metafiles", "--proxy", proxy.url,
            # Fragments are downloaded by yt-dlp itself, through the proxy: ffmpeg only
            # ever works on local files.
            "--downloader", "native",
            "--cache-dir", str(self.settings.tmp_dir / "yt-dlp-cache"),
            "--no-js-runtimes",
        ]  # fmt: skip
        if self.tools.js_runtime:
            args += ["--js-runtimes", self.tools.js_runtime]
        return args

    async def _run(self, args: list[str], proxy: GuardedProxy, cwd: Path) -> tuple[str, str]:
        """Run yt-dlp; ``(stdout, stderr)``. Raises ``FetchError`` on failure."""
        home = self.settings.tmp_dir / "yt-dlp-home"
        home.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        }
        proc = await asyncio.create_subprocess_exec(
            *self.command, *self._base_args(proxy), *args,
            cwd=cwd, env=env, start_new_session=True,  # ffmpeg is killed with it
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        timeout = self.settings.media.timeout_s
        try:
            async with asyncio.timeout(timeout):
                out, err = await proc.communicate()
        except TimeoutError:
            raise FetchError(f"yt-dlp gave up after {timeout:.0f}s") from None
        finally:
            if proc.returncode is None:  # timed out, or the job was cancelled
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        if proxy.blocked:
            raise PermanentFetchError(f"blocked by the SSRF guard: {proxy.blocked[0]}")
        if proxy.over_limit:
            raise PermanentFetchError(
                f"more than {self.settings.media.max_mb} MB downloaded; raise media.max_mb"
            )
        if proc.returncode != 0:
            raise _error(err.decode("utf-8", "replace"))
        return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    # -- the snapshot ---------------------------------------------------------------

    def _result(
        self,
        url: str,
        info: dict[str, Any],
        plan: _Plan,
        staging: Path,
        captions_lang: str | None,
        automatic: bool,
        reused: Artifact | None,
        output: str,
    ) -> SnapshotResult:
        artifacts: list[Artifact] = []
        meta: dict[str, Any] = {
            "site": info.get("webpage_url_domain") or info.get("extractor_key"),
            "video_id": info.get("id"),
            "channel": info.get("channel") or info.get("uploader"),
            "channel_url": info.get("channel_url") or info.get("uploader_url"),
            "duration_s": int(info["duration"]) if info.get("duration") else None,
            "published_at": _date(info.get("upload_date")),
            "language": info.get("language") or captions_lang,
        }
        captions = next(iter(sorted(staging.glob("captions.*"))), None)
        cues = parse_vtt(captions.read_text(encoding="utf-8", errors="replace")) if (
            captions and captions.suffix == ".vtt"
        ) else []  # fmt: skip
        meta["captions"] = ("automatic" if automatic else "manual") if cues else "none"

        content = staging / "content.md"
        content.write_text(_content_markdown(info, url, cues), encoding="utf-8")
        artifacts.append(describe_file(content, "content"))
        info_path = staging / "info.json"
        trimmed = {k: v for k, v in info.items() if k not in _DROPPED_INFO_KEYS}
        info_path.write_text(json.dumps(trimmed, indent=2, ensure_ascii=False), encoding="utf-8")
        artifacts.append(describe_file(info_path, "info"))
        if captions:
            artifacts.append(describe_file(captions, "captions"))
        if thumbnail := next(iter(sorted(staging.glob("thumbnail.*"))), None):
            artifacts.append(describe_file(thumbnail, "thumbnail"))

        media = next((p for p in sorted(staging.glob("media.*")) if _finished(p)), None)
        if plan.keep != MediaLevel.TRANSCRIPT and media is not None:
            height = _height(staging, reused) if plan.keep == MediaLevel.VIDEO else None
            extra: dict[str, Any] = {"keep": plan.keep.value}
            if plan.keep == MediaLevel.VIDEO:
                extra.update(max_height=plan.height, height=height)
            artifacts.append(describe_file(media, "media", **extra))
            meta["media_kept"] = plan.keep.value
            meta["media_height"] = height
        elif plan.keep != MediaLevel.TRANSCRIPT:
            plan.skipped = (
                f"larger than {self.settings.media.max_mb} MB"
                if "max-filesize" in output or "larger than max" in output.lower()
                else "no file could be downloaded"
            )
        meta["media_skipped"] = plan.skipped
        for leftover in staging.iterdir():  # partial downloads, the height file
            if leftover.is_file() and leftover.name not in {a.path for a in artifacts}:
                leftover.unlink()
        return SnapshotResult(
            artifacts=artifacts, meta=meta, title=info.get("title"), text_role="content"
        )


def _pick_captions(info: dict[str, Any]) -> tuple[str | None, bool]:
    """One captions track: written by a person in the video's language if there is one,
    else the automatic track in the original language. ``(language, automatic)``."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    language = info.get("language")
    if language:
        for key in (language, *sorted(k for k in manual if k.split("-")[0] == language)):
            if key in manual:
                return key, False
        for key in (f"{language}-orig", language):
            if key in auto:
                return key, True
    if manual:
        return next(iter(manual)), False
    original = [k for k in auto if k.endswith("-orig")]
    if original:
        return original[0], True
    return None, False


def _content_markdown(info: dict[str, Any], url: str, cues: list[Any]) -> str:
    title = " ".join(str(info.get("title") or "Untitled").split())
    lines = [f"# {title}", ""]
    facts = [
        str(info.get("channel") or info.get("uploader") or ""),
        timestamp(info["duration"]) if info.get("duration") else "",
        _date(info.get("upload_date")) or "",
    ]
    if any(facts):
        lines += [" · ".join(f for f in facts if f), ""]
    description = str(info.get("description") or "").strip()
    if description:
        lines += ["## Description", "", plain_text_markdown(description), ""]
    if cues:
        lines += ["## Transcript", "", transcript_markdown(cues, url, info.get("chapters")), ""]
    elif info.get("chapters"):
        lines += ["## Chapters", ""]
        lines += [
            f"- {timestamp(float(c.get('start_time') or 0))} {c.get('title', '')}"
            for c in info["chapters"]
        ]
        lines.append("")
    return "\n".join(lines)


def _date(value: Any) -> str | None:
    text = str(value or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if re.fullmatch(r"\d{8}", text) else None


_INTERMEDIATE = re.compile(r"^media\.f[\w-]+\.")  # one stream, before merging


def _finished(path: Path) -> bool:
    name = path.name
    return not name.endswith((".part", ".ytdl", ".temp")) and not _INTERMEDIATE.match(name)


def _height(staging: Path, reused: Artifact | None) -> int | None:
    if reused is not None:
        height = (reused.model_extra or {}).get("height")
        return height if isinstance(height, int) else None
    try:
        text = (staging / "media-height.txt").read_text().split()
        return int(text[-1]) if text else None
    except OSError, ValueError:
        return None


def _error(stderr: str) -> FetchError:
    lines = [line for line in stderr.splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR:")]
    text = (errors or lines or ["yt-dlp failed"])[-1].removeprefix("ERROR:").strip()[:500]
    lower = text.lower()
    if any(marker in lower for marker in _BOT_CHECK):
        return FetchError(f"the site asks to prove this is not a bot: {text}")
    if any(marker in lower for marker in _PERMANENT):
        return PermanentFetchError(text)
    return FetchError(text)
