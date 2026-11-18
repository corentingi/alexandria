"""A stand-in for ``python -m yt_dlp`` in tests: no network, files written from fixtures.

Usage: ``fake_ytdlp.py <log_dir> <yt-dlp args...>``. Each run appends its arguments to
``<log_dir>/runs.jsonl``. The URL picks the behaviour: "playlist", "live", "private" and
"bot" in it give those answers; "tall" offers a 720p format; anything else is a short
video with captions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.200 --> 00:00:03.360
All right, so here we are, in front of the
elephants

00:00:05.318 --> 00:00:07.974
the cool thing about these guys is that they
have really long trunks
"""


def info(url: str) -> dict[str, object]:
    return {
        "_type": "video",
        "id": "jNQXAC9IVRw",
        "title": "Me at the zoo",
        "channel": "jawed",
        "channel_url": "https://www.youtube.com/channel/UC4QobU6STFB0P71PMvOGN5A",
        "duration": 19,
        "upload_date": "20050424",
        "language": "en",
        "webpage_url": url,
        "webpage_url_domain": "youtube.com",
        "description": "# not a heading\nThe first video.",
        "chapters": [
            {"start_time": 0, "end_time": 5, "title": "Intro"},
            {"start_time": 5, "end_time": 19, "title": "Trunks"},
        ],
        "subtitles": {"en": [{"ext": "vtt"}], "de": [{"ext": "vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt"}], "en-de": [{"ext": "vtt"}]},
        "formats": [{"format_id": "18", "url": "https://expiring.example/stream"}],
    }


def main() -> int:
    log_dir, args = Path(sys.argv[1]), sys.argv[2:]
    with (log_dir / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(args) + "\n")
    if "-J" in args:
        url = args[-1]
        if "private" in url:
            print("ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
                  file=sys.stderr)  # fmt: skip
            return 1
        if "bot" in url:
            print("ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot", file=sys.stderr)
            return 1
        if "playlist" in url:
            print(json.dumps({"_type": "playlist", "id": "PL1", "entries": []}))
            return 0
        data = info(url)
        if "tall" in url:  # a 720p file exists, but this fake always saves 360p
            data["formats"] = [
                {
                    "format_id": "18",
                    "width": 640,
                    "height": 360,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
                {
                    "format_id": "22",
                    "width": 1280,
                    "height": 720,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                },
            ]
        if "live" in url:
            data["live_status"] = "is_live"
        print(json.dumps(data))
        return 0
    if "--sub-langs" in args:
        lang = args[args.index("--sub-langs") + 1].replace("\\", "")
        Path(f"captions.{lang}.vtt").write_text(VTT)
    if "--write-thumbnail" in args:
        Path("thumbnail.jpg").write_bytes(b"\xff\xd8\xff\xe0 fake jpeg")
    if "--skip-download" not in args:
        audio = args[args.index("-f") + 1].startswith("bestaudio")
        name = "media.m4a" if audio else "media.mp4"
        Path(name).write_bytes(b"\x00" * 2048)
        Path("media.f137.mp4.part").write_bytes(b"\x00")  # a leftover stream
        if "--print-to-file" in args:
            Path(args[args.index("--print-to-file") + 2]).write_text("" if audio else "640 360\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
