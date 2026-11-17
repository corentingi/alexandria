from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from alexandria.config import Settings
from alexandria.fetchers.base import PermanentFetchError
from alexandria.fetchers.media import _error, _pick_captions
from alexandria.jobs import Worker
from alexandria.library import InvalidInput, Library, Update
from alexandria.models import FetchStatus, Kind, MediaChoice, MediaLevel
from alexandria.transcript import parse_vtt, plain_text_markdown, transcript_markdown
from alexandria.urls import detect

VIDEO = "https://youtu.be/jNQXAC9IVRw?si=share"


class TestDetect:
    @pytest.mark.parametrize(
        "url",
        [
            "https://youtu.be/jNQXAC9IVRw?si=abc&t=3",
            "https://m.youtube.com/watch?v=jNQXAC9IVRw&list=PL1&t=4s",
            "https://www.youtube.com/shorts/jNQXAC9IVRw",
            "https://www.youtube.com/embed/jNQXAC9IVRw",
            "https://www.youtube-nocookie.com/embed/jNQXAC9IVRw",
        ],
    )
    def test_every_youtube_link_gives_one_url(self, url: str) -> None:
        detected = detect(url)
        assert detected.kind == Kind.MEDIA
        assert detected.canonical_url == "https://www.youtube.com/watch?v=jNQXAC9IVRw"

    @pytest.mark.parametrize(
        ("url", "canonical"),
        [
            ("https://vimeo.com/123456?share=copy", "https://vimeo.com/123456"),
            ("https://player.vimeo.com/video/123456", "https://vimeo.com/123456"),
            ("https://www.dailymotion.com/video/x8abc_a-title", "https://www.dailymotion.com/video/x8abc"),
            ("https://dai.ly/x8abc", "https://www.dailymotion.com/video/x8abc"),
            ("https://www.twitch.tv/videos/42", "https://www.twitch.tv/videos/42"),
            ("https://soundcloud.com/artist/song?in=x", "https://soundcloud.com/artist/song"),
        ],
    )  # fmt: skip
    def test_other_sites(self, url: str, canonical: str) -> None:
        assert detect(url) == detect(canonical)
        assert detect(url).kind == Kind.MEDIA and detect(url).canonical_url == canonical

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/playlist?list=PL1",
            "https://www.youtube.com/@channel",
            "https://www.youtube.com/watch?v=short",
            "https://soundcloud.com/artist/sets",
            "https://example.com/video.mp4",
        ],
    )
    def test_pages_that_are_not_single_videos_stay_articles(self, url: str) -> None:
        assert detect(url).kind == Kind.ARTICLE

    def test_any_site_can_be_saved_as_media_when_asked(self) -> None:
        detected = detect("https://videos.example.org/w/abc", Kind.MEDIA)
        assert detected.kind == Kind.MEDIA


ROLLING = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%
 
hello<00:00:00.500><c> world</c><00:00:01.000><c> this</c>

00:00:02.000 --> 00:00:02.010 align:start position:0%
hello world this
 

00:00:02.010 --> 00:00:04.000 align:start position:0%
hello world this
is<00:00:02.500><c> a</c><00:00:03.000><c> test &amp; more</c>

00:02:10.000 --> 00:02:12.000
# a new minute
"""


class TestTranscript:
    def test_rolling_captions_read_once(self) -> None:
        cues = parse_vtt(ROLLING)
        assert cues[0].start == 0 and cues[-1].start == 130
        text = transcript_markdown(cues, "https://www.youtube.com/watch?v=jNQXAC9IVRw")
        assert text.count("hello world this") == 1
        assert "is a test & more" in text
        first, second = text.split("\n\n")
        assert first.startswith("[0:00](https://www.youtube.com/watch?v=jNQXAC9IVRw&t=0s)")
        assert second.startswith("[2:10](") and "# a new minute" in second

    def test_paragraphs_end_at_a_sentence_after_a_minute(self) -> None:
        vtt = "WEBVTT\n\n" + "\n\n".join(
            f"00:{m:02d}:{sec:02d}.000 --> 00:{m:02d}:{sec:02d}.900\n{text}"
            for m, sec, text in [(0, 0, "One."), (0, 59, "It is"), (1, 2, "dauntingly hard."),
                                 (1, 5, "Next"), (3, 30, "no stop")]
        )  # fmt: skip
        paragraphs = transcript_markdown(parse_vtt(vtt), None).split("\n\n")
        assert paragraphs == ["**0:00** One. It is dauntingly hard.", "**1:05** Next",
                              "**3:30** no stop"]  # fmt: skip

    def test_chapters_become_headings(self) -> None:
        chapters = [{"start_time": 0, "title": "Start"}, {"start_time": 60, "title": "# Later"}]
        text = transcript_markdown(parse_vtt(ROLLING), None, chapters)
        assert text.startswith("### Start\n\n**0:00** hello world this")
        assert "### \\# Later\n\n**2:10**" in text

    def test_description_cannot_make_headings(self) -> None:
        text = plain_text_markdown("# Big\n> quote\nline\n\n1. item")
        assert text == "\\# Big  \n\\> quote  \nline\n\n\\1. item"


class TestCaptionChoice:
    def test_manual_captions_in_the_video_language_first(self) -> None:
        info = {"language": "fr", "subtitles": {"en": [], "fr-FR": []},
                "automatic_captions": {"fr-orig": []}}  # fmt: skip
        assert _pick_captions(info) == ("fr-FR", False)

    def test_original_automatic_track_not_a_translation(self) -> None:
        info = {"language": "en", "automatic_captions": {"de": [], "en-orig": [], "en": []}}
        assert _pick_captions(info) == ("en-orig", True)
        info = {"automatic_captions": {"de": [], "fr-orig": []}}
        assert _pick_captions(info) == ("fr-orig", True)

    def test_none(self) -> None:
        assert _pick_captions({"subtitles": {"live_chat": []}}) == (None, False)


class TestErrors:
    def test_classification(self) -> None:
        assert isinstance(_error("ERROR: [youtube] x: Private video"), PermanentFetchError)
        assert isinstance(_error("WARNING: a\nERROR: Video unavailable"), PermanentFetchError)
        bot = _error("ERROR: [youtube] x: Sign in to confirm you\u2019re not a bot")
        assert not bot.permanent and "not a bot" in str(bot)
        assert not _error("ERROR: Connection reset by peer").permanent


# -- fetching through the library ------------------------------------------------------


@pytest.fixture
async def media_library(settings: Settings) -> AsyncIterator[Library]:
    lib = Library(settings)  # yt-dlp is the fake one (conftest)
    yield lib
    lib.close()


def _runs(tmp_path: Path) -> list[list[str]]:
    lines = (tmp_path / "fake-ytdlp" / "runs.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


class TestMediaFetch:
    async def test_transcript_by_default(self, media_library: Library, tmp_path: Path) -> None:
        result = await media_library.ingest(VIDEO, why="the first video")
        assert result.resource.kind == Kind.MEDIA
        assert result.resource.media == MediaChoice(keep=MediaLevel.TRANSCRIPT)
        await Worker(media_library).drain()
        r = media_library.get(result.resource.id)
        assert r.fetch_status == FetchStatus.READY, r.fetch_error
        assert r.title == "Me at the zoo" and r.slug == "me-at-the-zoo"
        assert r.meta["channel"] == "jawed" and r.meta["duration_s"] == 19
        assert r.meta["published_at"] == "2005-04-24" and r.meta["captions"] == "manual"
        roles = {a.role: a for a in r.snapshots[-1].artifacts}
        assert set(roles) == {"content", "info", "captions", "thumbnail"}
        assert roles["captions"].path == "captions.en.vtt"
        info = json.loads(media_library.files.read_artifact_text(r, "info") or "{}")
        assert "formats" not in info and info["title"] == "Me at the zoo"
        content = media_library.files.read_artifact_text(r, "content") or ""
        assert "### Trunks" in content and "\\# not a heading" in content
        hits = media_library.search("elephants")
        assert [row["id"] for _, row in hits] == [r.id]

        runs = _runs(tmp_path)
        assert len(runs) == 2 and "-J" in runs[0] and "--skip-download" in runs[1]
        for args in runs:
            proxy = args[args.index("--proxy") + 1]
            assert proxy.startswith("http://alexandria:") and "@127.0.0.1:" in proxy
            assert args[args.index("--downloader") + 1] == "native"
        assert runs[1][runs[1].index("--sub-langs") + 1] == "en"

    async def test_audio_needs_downloads(self, media_library: Library) -> None:
        assert media_library.media_levels() == ["transcript"]
        with pytest.raises(InvalidInput, match="transcript only"):
            await media_library.ingest(VIDEO, media=MediaChoice(keep=MediaLevel.AUDIO))

    async def test_audio_then_refetch_reuses_the_file(
        self, media_library: Library, settings: Settings, tmp_path: Path
    ) -> None:
        settings.media.downloads = True
        result = await media_library.ingest(VIDEO, media=MediaChoice(keep=MediaLevel.AUDIO))
        await Worker(media_library).drain()
        r = media_library.get(result.resource.id)
        media = r.artifact("media")
        assert media is not None and media.path == "media.m4a" and media.mime == "audio/mp4"
        assert r.meta["media_kept"] == "audio"
        first = media_library.files.snapshot_dir(r.id, r.current_snapshot or "") / media.path
        files = {a.path for a in r.snapshots[-1].artifacts}
        assert not any(".part" in f for f in files)

        await media_library.refetch(r.id)
        await Worker(media_library).drain()
        r = media_library.get(r.id)
        assert len(r.snapshots) == 2
        second = media_library.files.snapshot_dir(r.id, r.current_snapshot or "") / "media.m4a"
        assert second.stat().st_ino == first.stat().st_ino  # hard link, not downloaded again
        assert "--skip-download" in _runs(tmp_path)[-1]

    async def test_video_with_its_height(self, media_library: Library, settings: Settings,
                                         tmp_path: Path) -> None:  # fmt: skip
        settings.media.downloads = True
        choice = MediaChoice(keep=MediaLevel.VIDEO, max_height=480)
        result = await media_library.ingest(VIDEO, media=choice)
        await Worker(media_library).drain()
        r = media_library.get(result.resource.id)
        media = r.artifact("media")
        assert media is not None and media.path == "media.mp4"
        assert r.meta["media_kept"] == "video" and r.meta["media_height"] == 360
        args = _runs(tmp_path)[-1]
        assert args[args.index("-f") + 1] == "b[height<=480]/w"  # no ffmpeg: one file

    async def test_changing_the_choice(self, media_library: Library, settings: Settings) -> None:
        result = await media_library.ingest(VIDEO)
        with pytest.raises(InvalidInput):
            video = Update(media=MediaChoice(keep=MediaLevel.VIDEO))
            await media_library.update(result.resource.id, video)
        settings.media.downloads = True
        r = await media_library.update(
            result.resource.id, Update(media=MediaChoice(keep=MediaLevel.VIDEO, max_height=1080))
        )
        assert r.media == MediaChoice(keep=MediaLevel.VIDEO, max_height=1080)
        assert media_library.get(r.id).media == r.media  # in resource.md

    async def test_long_videos_keep_their_transcript(
        self, media_library: Library, settings: Settings
    ) -> None:
        settings.media.downloads = True
        settings.media.max_duration_min = 0
        result = await media_library.ingest(VIDEO, media=MediaChoice(keep=MediaLevel.AUDIO))
        await Worker(media_library).drain()
        r = media_library.get(result.resource.id)
        assert r.fetch_status == FetchStatus.READY and r.artifact("media") is None
        assert r.meta["media_skipped"] == "longer than 0 minutes"

    @pytest.mark.parametrize(
        ("url", "message", "status"),
        [
            ("https://www.youtube.com/watch?v=privateabcd", "Private video", FetchStatus.FAILED),
            ("https://www.youtube.com/watch?v=livexxxxxxx", "live stream", FetchStatus.FAILED),
            ("https://videos.example.org/playlist/1", "playlist", FetchStatus.FAILED),
            ("https://www.youtube.com/watch?v=botbotbotbo", "not a bot", FetchStatus.PENDING),
        ],
    )
    async def test_failures(
        self, media_library: Library, url: str, message: str, status: FetchStatus
    ) -> None:
        result = await media_library.ingest(url, kind=Kind.MEDIA)
        job = media_library.index.claim_job()
        assert job is not None
        with pytest.raises(Exception, match=message):
            await media_library._job_fetch(job)
        r = media_library.get(result.resource.id)
        assert r.fetch_status == status and message in (r.fetch_error or "")
