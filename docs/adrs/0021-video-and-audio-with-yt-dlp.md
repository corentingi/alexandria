# ADR-0021 — Video and audio pages are fetched with yt-dlp

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

A YouTube or Vimeo link saved as an article keeps a page of scripts and no words: what
is worth keeping is what the video says. yt-dlp supports about 1,800 sites, gives the
details (channel, date, duration, chapters), the captions, the thumbnail and the files.
For YouTube it now needs a JavaScript runtime to see every format, and ffmpeg to join
the separate video and audio streams above 360p.

## Decision

- A new kind, `media`. Single videos on YouTube (including youtu.be, Shorts, embeds),
  Vimeo, Dailymotion, Twitch and SoundCloud are detected and get one URL, so every way
  of linking a video saves it once. Playlists and channels stay articles. Any other site
  yt-dlp supports is saved as media when the kind is chosen explicitly: matching every
  link against yt-dlp's site rules on each save is slow and catches far too much.
- Each media resource records what to keep, in `resource.md` (`media.keep`, a human
  field): `transcript` (default), `audio` or `video`, and for video a height cap (360 to
  2160, default `media.max_height`, 720). The choice can change later; it applies from
  the next fetch. A bulk import takes one choice for all the videos among its
  links (the other links ignore it); the import stays its own tab, since one link gets
  live checks and a redirect to its page that thousands of links cannot.
- A snapshot always holds `content.md` (title, description, transcript), `info.json`
  (details without the stream URLs, which expire), the captions and the thumbnail. The
  transcript is what search and agents read. The audio or video file is added when asked.
- Captions: one track, a person's in the video's language first, else the automatic track
  in the original language. Asking for every variant gets HTTP 429 from YouTube.
- Audio is kept as served (m4a when there is one): no conversion. Video takes the
  highest resolution up to the cap, the resolution being the shorter side (a vertical
  1080x1920 Short is 1080p), then prefers H.264 and AAC in MP4, which every browser plays.
- yt-dlp runs as a child process (`python -m yt_dlp`), killed after `media.timeout_s`.
  Its network goes through a guarded proxy (ADR-0022), and stream fragments are
  downloaded by yt-dlp itself (`--downloader native`), so ffmpeg only touches local files.
- Limits: `media.max_mb` per file (4 GB), and videos longer than
  `media.max_duration_min` (6 h) keep their transcript only. A skipped file is noted in
  `meta.media_skipped`; the fetch still succeeds.
- A re-fetch hard-links the previous media file when the same thing is asked and no
  higher resolution is offered under the cap, instead of downloading gigabytes again.
- Private, removed, members-only and age-restricted videos fail at once. A bot check
  ("confirm you're not a bot") is retried later. Live streams fail until they end.
- One image: Deno and ffmpeg ship in it. Audio and video need ffmpeg (`media.downloads`
  defaults to "when ffmpeg is installed"); without it the server keeps transcripts only,
  which leaves room for a slim image built without them.
- No cookies: videos that need a logged-in account are not supported.

## Cost

- The image grows by Deno and ffmpeg (a few hundred MB).
- yt-dlp follows sites that change often; a pinned version stops working on YouTube
  within weeks or months. How to keep it current is still to decide.
- A transcript is only as good as the captions: a video without them is kept with its
  description only (speech-to-text is not done).
- Saving videos uses disk quickly: about 0.5–1 GB per hour at 720p.
