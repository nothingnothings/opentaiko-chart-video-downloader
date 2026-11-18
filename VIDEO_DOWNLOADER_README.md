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
| Python 3.9+ with tkinter | [python.org](https://www.python.org/downloads/) | Windows installer includes tkinter; Linux/Steam Deck: `sudo pacman -S python tk` |
| **yt-dlp** | [github.com/yt-dlp/yt-dlp/releases](https://github.com/yt-dlp/yt-dlp/releases) → `yt-dlp.exe` (Windows) / `yt-dlp_linux` (Linux) | Auto-detected: settings → next to this script → PATH → `python -m yt_dlp` |
| **ffmpeg** | [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) or `winget install ffmpeg`; Linux: `sudo pacman -S ffmpeg` | Auto-detected: settings → next to this script → PATH |

Both tools can also be set manually in the GUI (the `…` browse buttons).

> **Tip:** `yt-dlp.exe` is already provided next to the script in this
> workspace, but it is a **Windows-only** binary. Keep it updated occasionally:
> `yt-dlp -U` (or re-download).

### Steam Deck / Linux

Work in **Desktop Mode**. The bundled `.exe` cannot run there; install the
native tools instead:

```
sudo pacman -S python tk ffmpeg yt-dlp
```

If pacman reports a read-only filesystem, run `sudo steamos-readonly disable`
first (and re-enable it afterwards). Use `python3` instead of `python` if your
system doesn't provide the `python` alias.

Then run `python opentaiko_video_downloader.py`. The app finds `yt-dlp` and
`ffmpeg` on `PATH` automatically. Alternatively, download `yt-dlp_linux` from
the releases page, place it next to the script and run `chmod +x yt-dlp_linux`.
The Windows `yt-dlp.exe` can stay in the folder — it is ignored on Linux.
Each candidate is checked with `--version`/`-version` at startup, and any
rejection reason is printed in the log panel.

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

* **`yt-dlp.exe` fails on Linux / Steam Deck** — that file is a Windows
  executable. Install native `yt-dlp` (`sudo pacman -S yt-dlp` or pip), or use
  `yt-dlp_linux` with `chmod +x`, as described above.
* **"Sign in to confirm you're not a bot"** / age-restricted videos —
  YouTube throttling. Adding cookies usually fixes it: a `yt-dlp.conf` next to
  `yt-dlp.exe` on Windows, or `~/.config/yt-dlp/config` on Linux
  (`--cookies-from-browser firefox` or `--cookies browser.txt`). The script
  doesn't override your yt-dlp configuration.
* **Missing JavaScript runtime error from yt-dlp** — install
  [Deno](https://deno.com) or Node.js (yt-dlp uses one of them to solve
  YouTube's JS challenges).
* **Search works but download fails** — check the log; it contains yt-dlp's
  full output. Stale yt-dlp versions are the usual cause → `yt-dlp -U` or
  `python -m pip install -U yt-dlp`.
* **Video doesn't show in game** — make sure BGMOVIE display is enabled in
  OpenTaiko (Settings → System → BGMOVIE) and the file is in the chart's
  folder (it is named in the `.tja`, visible in the *Video* column).
