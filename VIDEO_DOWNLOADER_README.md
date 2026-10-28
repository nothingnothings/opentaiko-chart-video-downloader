# OpenTaiko BGMOVIE Downloader

A Python GUI tool for **OpenTaiko** that scans your `Songs` folder, lists every
chart (`.tja`) grouped by its category (`box.def` folders), and lets you
download background videos from YouTube for the songs you choose.

For each selected song it:

1. searches YouTube for the song title and shows you the top results
   (you pick one — or you paste a URL yourself),
2. downloads the video into the song's folder as `<chart name>.mp4`
   (yt-dlp only *resolves* which streams to grab — the actual download and
   muxing into one MP4 is done by **ffmpeg**, stream-copied, no re-encode),
3. links the video into the chart by writing a `BGMOVIE:` header line into
   the `.tja` file (and optionally `MOVIEOFFSET:`), preserving the file's
   original text encoding (UTF-8 / Shift-JIS / UTF-16) and line endings.

OpenTaiko then plays the video behind the notes during gameplay
(Settings → System → BGMOVIE).

---

## Requirements

| Tool | Where | Notes |
|------|-------|-------|
| Python 3.9+ with tkinter | [python.org](https://www.python.org/downloads/) | Standard Windows installer includes tkinter |
| **yt-dlp** | [github.com/yt-dlp/yt-dlp/releases](https://github.com/yt-dlp/yt-dlp/releases) → `yt-dlp.exe` | Auto-detected: next to this script → PATH → `pip install yt-dlp` |
| **ffmpeg** | [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) or `winget install ffmpeg` | Auto-detected: next to this script → PATH |

Both tools can also be set manually in the GUI (the `…` browse buttons).

> **Tip:** `yt-dlp.exe` is already provided next to the script in this
> workspace. Keep it updated occasionally: `yt-dlp -U` (or re-download).

---

## Usage

```
python opentaiko_video_downloader.py
```

1. The **Songs folder** is auto-detected (the OpenTaiko `Songs` directory next
   to / inside the script's folder). Change it with *Browse…* if needed and
   hit **Rescan**.
2. The tree shows **categories → song folders → charts**. Rows are colored:
   green = video linked and present, red = `BGMOVIE` points to a missing
   file, gray = no video yet.
3. Click rows (or press Space) to check them. Use the **filter** box to find
   songs quickly, or **Select shown without video** to grab everything
   missing at once.
4. Click **Download videos for N selected songs**. For each song a dialog
   opens with YouTube search results (title / channel / duration) — double
   click the right one, or paste any YouTube URL. Choose:
   * **Download selected** — downloads and links it,
   * **Skip this song**, or
   * **Cancel batch**.
5. Optional per song: **MOVIEOFFSET** (seconds) shifts when the video starts,
   for videos whose sound/intro doesn't line up with the chart. Leave blank
   to keep whatever the chart already has.
6. Prefer no dialogs at all? Tick **Auto-pick first result** (or the
   checkbox inside the dialog) and every song gets the top search result.

Everything is logged in the panel at the bottom. The download command used
for each video is also logged, e.g.:

```
yt-dlp --no-playlist --newline -t mp4 -S res:1080
       --downloader ffmpeg --downloader-args ffmpeg_i:...
       --ffmpeg-location <dir> -o <song folder>\<chart>.%(ext)s -- <URL>
```

### Headless modes

```
python opentaiko_video_downloader.py --scan-only [SongsFolder]   # print the library
python opentaiko_video_downloader.py --self-test                 # quick sanity checks
```

---

## How the video is linked

OpenTaiko reads a `BGMOVIE:<file>` line in the `.tja` global header and plays
that file (from the chart's own folder) behind the notes, decoupled from the
song audio — so the music you hear is still the chart's own `WAVE` file.

The script inserts, before the first `COURSE:` line:

```
BGMOVIE:<chart name>.mp4
MOVIEOFFSET:<seconds>      (only if you provided one)
```

Notes:

* The video file is named after the **chart file** (`Katharsis.tja` →
  `Katharsis.mp4`), so several charts in one folder can each have their own
  video.
* Re-downloading a video replaces the old file and updates the tag.
* Files are edited in place with their original encoding; if a video path
  can't be represented in the chart's encoding (very rare), the file is
  re-saved as UTF-8 and a note is logged.

---

## Troubleshooting

* **"Sign in to confirm you're not a bot"** / age-restricted videos —
  YouTube throttling. Adding cookies to a `yt-dlp.conf` next to `yt-dlp.exe`
  (`--cookies-from-browser firefox` or `--cookies browser.txt`) usually fixes
  it; the script doesn't override your yt-dlp configuration.
* **Missing JavaScript runtime error from yt-dlp** — install
  [Deno](https://deno.com) or Node.js (yt-dlp uses one of them to solve
  YouTube's JS challenges).
* **Search works but download fails** — check the log; it contains yt-dlp's
  full output. Stale yt-dlp versions are the usual cause → `yt-dlp -U`.
* **Video doesn't show in game** — make sure BGMOVIE display is enabled in
  OpenTaiko (Settings → System → BGMOVIE) and the file is in the chart's
  folder (it is named in the `.tja`, visible in the *Video* column).
