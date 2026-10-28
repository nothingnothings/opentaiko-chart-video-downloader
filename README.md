# OpenTaiko BGMOVIE Downloader

A Python GUI tool that scans your OpenTaiko `Songs` folder, lists every chart
(`.tja`) grouped by category (`box.def` folders), and lets you download
background videos from YouTube for the songs you choose. Downloaded videos are
linked into their charts via the `BGMOVIE:` tag so OpenTaiko plays them behind
the notes during gameplay.

## What it does

1. **Scans** your `Songs` folder recursively, grouping charts by the
   category detected from each folder's `box.def`.
2. **Lists** every chart in a tree view, color-coded by video status:
   * green — `BGMOVIE` is set and the video file exists,
   * red — `BGMOVIE` points to a missing file,
   * gray — no video linked yet.
3. **Lets you select** specific songs for download (multi-select, filter box,
   "Select all / Select none / Select shown without video").
4. **Searches YouTube** for each selected song's title and shows you the top
   results (title, channel, duration) — you pick one, paste a URL yourself, or
   enable auto-pick to skip the dialog entirely.
5. **Downloads** the video into the song's folder as `<chart name>.mp4`
   (H.264 video + AAC audio in an MP4 container, stream-copied — no
   re-encoding). The actual download and muxing is performed by **ffmpeg**,
   while yt-dlp only resolves which streams to fetch.
6. **Links** the video by writing/updating `BGMOVIE:<filename>` (and optional
   `MOVIEOFFSET:<seconds>`) into the chart's global header, preserving the
   file's original text encoding (UTF-8 / Shift-JIS / UTF-16) and line endings.

OpenTaiko then plays the video behind the notes during gameplay
(enable via **Settings → System → BGMOVIE**).

## Installation

### 1. Install Python
- Windows: download and install [Python 3.9+](https://www.python.org/downloads/).
- During installation, check **"Add Python to PATH"** and make sure **tcl/tk**
  (tkinter) is included — the standard Windows installer includes it by default.

Verify:
```
python --version
```

### 2. Install ffmpeg
ffmpeg performs the actual download and muxing into MP4.

**Option A — winget (recommended on Windows):**
```
winget install ffmpeg
```

**Option B — manual:**
- Download a release build from [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/)
  (`ffmpeg-release-essentials.zip`).
- Extract and add the `bin` folder to your PATH.

Verify:
```
ffmpeg -version
```

### 3. Install yt-dlp
yt-dlp resolves YouTube video URLs and picks the right streams.

**Option A — standalone executable (included here):**
- A copy of `yt-dlp.exe` is already placed next to this script in the
  repository. The tool auto-detects it from the script's folder, so nothing
  else is needed. Keep it updated occasionally:
  ```
  yt-dlp -U
  ```

**Option B — pip:**
```
pip install yt-dlp
```
(yt-dlp installed via pip is also auto-detected.)

**Option C — manual:**
- Download `yt-dlp.exe` from
  [github.com/yt-dlp/yt-dlp/releases](https://github.com/yt-dlp/yt-dlp/releases)
  and place it next to `opentaiko_video_downloader.py`, or somewhere on your
  PATH.

### 4. Get the script
- `opentaiko_video_downloader.py` — the downloader GUI.
- `yt-dlp.exe` — already included in this workspace.

No installer is needed; just keep both files together and run the script with
Python.

## Quick start

```
python opentaiko_video_downloader.py
```

1. The **Songs folder** is auto-detected (the OpenTaiko `Songs` directory next
   to or inside the script's folder). Change it with **Browse…** if needed and
   hit **Rescan**.
2. The tree shows **categories → song folders → charts**. Use the **filter**
   box to find songs quickly, or click **Select shown without video** to grab
   everything missing at once.
3. Click rows (or press **Space**) to check the songs you want.
4. Click **Download videos for N selected songs**.
5. For each song, a dialog shows YouTube search results — double-click the
   right one, or paste any YouTube URL below. Choose **Download selected**,
   **Skip this song**, or **Cancel batch**.
6. Optional per song: enter a **MOVIEOFFSET** (in seconds) to shift when the
   video starts if its intro doesn't line up with the chart.
7. Everything is logged in the panel at the bottom.

## Headless / CLI modes

```
python opentaiko_video_downloader.py --scan-only [SongsFolder]
    # Print the library: categories, charts, and video status.

python opentaiko_video_downloader.py --self-test
    # Run built-in sanity checks (encoding round-trips, header edits,
    # tool detection, real library scan).
```

## How the video is linked

OpenTaiko reads a `BGMOVIE:<file>` line in a `.tja` chart's global header and
plays that file (from the chart's own folder) behind the notes, decoupled from
the chart's own `WAVE` audio — so the music you hear is still the chart's song.

The script inserts, before the first `COURSE:` line:

```
BGMOVIE:<chart name>.mp4
MOVIEOFFSET:<seconds>   # only if you provided one
```

Notes:
- The video file is named after the **chart file** (`Katharsis.tja` →
  `Katharsis.mp4`), so several charts in one folder can each have their own
  video.
- Re-downloading replaces the old file and updates the tag.
- Files are edited in place with their original encoding; if a video path
  cannot be stored in the chart's encoding (very rare), the file is re-saved
  as UTF-8 and a note is logged.

## Tool auto-detection order

Both yt-dlp and ffmpeg are located automatically, in this order:

1. The path you entered in the GUI settings fields (persisted across runs).
2. The folder containing `opentaiko_video_downloader.py`.
3. Your system PATH.
4. For yt-dlp only: `python -m yt_dlp` (if installed via pip).

## Troubleshooting

| Problem | Likely cause / fix |
|---------|--------------------|
| **"Sign in to confirm you're not a bot"** / age-restricted video | YouTube throttling. Add cookies to a `yt-dlp.conf` next to `yt-dlp.exe` (`--cookies-from-browser firefox` or `--cookies browser.txt`). |
| **Missing JavaScript runtime error from yt-dlp** | Install [Deno](https://deno.com/) or Node.js — yt-dlp uses one to solve YouTube's JS challenges. |
| **Download fails but search works** | Stale yt-dlp version is the usual cause → `yt-dlp -U`. The full yt-dlp output is shown in the log panel. |
| **Video doesn't show in game** | Make sure BGMOVIE is enabled in OpenTaiko (**Settings → System → BGMOVIE**) and the `.mp4` sits in the chart's folder. |
| **Window freezes during a download** | Should be fixed in the current build — progress output is throttled so the UI stays responsive. If it happens again, close the app and re-run. |

## Requirements

| Tool | Required | Notes |
|------|----------|-------|
| Python 3.9+ + tkinter | Yes | Standard Windows installer includes tkinter |
| ffmpeg | Yes | Downloads and muxes the MP4 (H.264 + AAC) |
| yt-dlp | Yes | Resolves YouTube stream URLs |

## License

This is an unofficial community tool for OpenTaiko. YouTube downloads are
subject to YouTube's Terms of Service; use responsibly.