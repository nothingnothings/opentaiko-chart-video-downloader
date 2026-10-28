#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenTaiko BGMOVIE Downloader
============================

A GUI tool that scans an OpenTaiko "Songs" folder, lists every chart (.tja)
grouped by category (box.def folders), lets you choose which songs should get
a background video, and for each selected song:

  1. searches YouTube for the song title (you pick the best result, or paste
     a URL yourself),
  2. downloads the video into the song's folder as "<chart name>.mp4"
     (yt-dlp only resolves which streams to grab — the actual download and
     muxing into a single H.264+AAC MP4 is done by ffmpeg),
  3. links the video into the chart by writing/updating a BGMOVIE header line
     (and optionally MOVIEOFFSET) in the .tja file, preserving the file's
     original text encoding and line endings.

Requirements
------------
* Python 3.9+ with tkinter (standard Windows Python includes it).
* yt-dlp    - https://github.com/yt-dlp/yt-dlp/releases  (yt-dlp.exe)
* ffmpeg    - https://www.gyan.dev/ffmpeg/builds/  or  `winget install ffmpeg`

Both tools are auto-detected from (in order): the settings fields in the GUI,
the folder containing this script, then PATH. yt-dlp installed via
`pip install yt-dlp` is also detected.

Command line:
    python opentaiko_video_downloader.py                # start the GUI
    python opentaiko_video_downloader.py --scan-only    # print the library
    python opentaiko_video_downloader.py --self-test    # headless checks
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

APP_TITLE = "OpenTaiko BGMOVIE Downloader"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "opentaiko_video_downloader_config.json"

VIDEO_EXTS = (".mp4", ".avi", ".mkv", ".webm", ".mov", ".mpg", ".mpeg",
              ".m4v", ".wmv", ".flv")
SEARCH_RESULTS = 8

# (label, value) — value None means "no resolution cap"
MAX_RES_CHOICES = (
    ("Best available", None),
    ("1080p (recommended)", 1080),
    ("720p", 720),
    ("2160p (4K)", 2160),
)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Header lines: KEY : value   (OpenTaiko splits on the first ASCII ':')
HDR_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")
COURSE_RE = re.compile(r"^COURSE\s*:", re.I)
START_RE = re.compile(r"^#START\b", re.I)


class ToolError(Exception):
    """Raised when an external tool (yt-dlp) failed."""


# --------------------------------------------------------------------------
# 1. Text / encoding helpers
# --------------------------------------------------------------------------

def sniff_encoding(raw: bytes) -> str:
    """Mirror OpenTaiko's CJudgeTextEncoding (ReadJEnc) behaviour closely
    enough: BOM check, then UTF-8, then Shift-JIS (CP932), then latin-1."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        raw.decode("cp932")
        return "cp932"
    except UnicodeDecodeError:
        pass
    return "latin-1"


def natural_key(text: str):
    """Human sort: '02 - X' before '10 - Y'."""
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", text)
    )


# --------------------------------------------------------------------------
# 2. TJA parsing / editing
# --------------------------------------------------------------------------

@dataclass
class Chart:
    path: Path
    song_dir: Path
    categories: tuple
    codec: str
    newline: str
    trailing_newline: bool
    lines: list
    boundary: int            # index of first COURSE:/#START line (global header end)
    title: str
    wave: str
    bgmovie: str
    movieoffset: str
    error: str = ""
    iid: str = ""
    selected: bool = False

    @property
    def folder_name(self) -> str:
        return self.song_dir.name

    @property
    def stem(self) -> str:
        return self.path.stem

    def video_state(self):
        """Return (tag, text): ok / miss / none / err."""
        if self.error:
            return "err", "✗ read error"
        bg = self.bgmovie.strip()
        if not bg:
            return "none", "— none"
        try:
            exists = (self.song_dir / bg).is_file()
        except (OSError, ValueError):
            exists = False
        if exists:
            return "ok", "✓ " + Path(bg).name
        return "miss", "✗ file missing"


def parse_header_line(line: str):
    """Return (KEY_UPPER, value) for 'KEY:value' lines, else None."""
    m = HDR_RE.match(line)
    if not m:
        return None
    return m.group(1).upper(), m.group(2)


def find_global_boundary(lines) -> int:
    for i, ln in enumerate(lines):
        s = ln.strip()
        if COURSE_RE.match(s) or START_RE.match(s):
            return i
    return len(lines)


def first_header_value(lines, key: str, lo: int = 0, hi: int | None = None) -> str:
    hi = len(lines) if hi is None else min(hi, len(lines))
    for i in range(lo, hi):
        kv = parse_header_line(lines[i])
        if kv and kv[0] == key and kv[1].strip():
            return kv[1].strip()
    return ""


def all_header_indices(lines, key: str):
    return [i for i, ln in enumerate(lines)
            if (kv := parse_header_line(ln)) and kv[0] == key]


def load_chart(tja_path: Path, song_dir: Path | None = None,
               categories: tuple = ()) -> Chart:
    song_dir = Path(song_dir) if song_dir else tja_path.parent
    raw = tja_path.read_bytes()
    codec = sniff_encoding(raw)
    text = raw.decode(codec)
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    boundary = find_global_boundary(lines)

    title = (first_header_value(lines, "TITLE", 0, boundary)
             or first_header_value(lines, "TITLE"))
    wave = (first_header_value(lines, "WAVE", 0, boundary)
            or first_header_value(lines, "WAVE"))
    bgmovie = (first_header_value(lines, "BGMOVIE", 0, boundary)
               or first_header_value(lines, "BGMOVIE"))
    movieoffset = (first_header_value(lines, "MOVIEOFFSET", 0, boundary)
                   or first_header_value(lines, "MOVIEOFFSET"))
    return Chart(
        path=tja_path, song_dir=song_dir, categories=categories,
        codec=codec, newline=newline, trailing_newline=trailing,
        lines=lines, boundary=boundary, title=title, wave=wave,
        bgmovie=bgmovie, movieoffset=movieoffset,
    )


def write_tja_headers(chart: Chart, bgmovie: str | None = None,
                      movieoffset: str | None = None) -> str:
    """Insert/replace BGMOVIE (and MOVIEOFFSET) header lines.

    OpenTaiko's TryParseGlobalHeader() is applied to *every* line of the file,
    so a BGMOVIE anywhere acts as a global header.  We therefore replace every
    existing line of a key we are writing, and insert new ones at the end of
    the global header region (just before the first COURSE:/#START line).
    """
    lines = list(chart.lines)
    msgs = []
    inserts = []

    def apply_edit(key: str, value: str):
        new_line = f"{key}:{value}"
        idxs = all_header_indices(lines, key)
        if idxs:
            changed = False
            for i in idxs:
                if lines[i] != new_line:
                    lines[i] = new_line
                    changed = True
            msgs.append(f"{key} updated" if changed else f"{key} already set")
        else:
            inserts.append(new_line)
            msgs.append(f"{key} added")

    if bgmovie is not None:
        apply_edit("BGMOVIE", bgmovie)
    if movieoffset is not None:
        apply_edit("MOVIEOFFSET", movieoffset)

    pos = chart.boundary if 0 <= chart.boundary <= len(lines) else len(lines)
    for new_line in inserts:
        lines.insert(pos, new_line)
        pos += 1

    new_text = chart.newline.join(lines)
    if chart.trailing_newline:
        new_text += chart.newline

    try:
        data = new_text.encode(chart.codec)
    except (UnicodeEncodeError, LookupError):
        data = new_text.encode("utf-8-sig")
        msgs.append("file re-encoded to UTF-8 to store the new header")
    chart.path.write_bytes(data)
    return "; ".join(msgs)


def proposed_video_name(chart: Chart) -> str:
    """Video file name: '<chart stem>.mp4' (matches the companion-file rules
    OpenTaiko uses as a fallback).  Falls back to a safe name if the stem
    cannot be stored with the chart's text encoding."""
    name = chart.stem + ".mp4"
    try:
        name.encode(chart.codec)
        return name
    except (UnicodeEncodeError, LookupError):
        pass
    base = "bgvideo"
    name = base + ".mp4"
    n = 2
    own = Path(chart.bgmovie or "").name
    while (chart.song_dir / name).exists() and own != name and n <= 99:
        name = f"{base}{n}.mp4"
        n += 1
    return name


def read_box_title(boxdef: Path) -> str:
    try:
        raw = boxdef.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    codec = sniff_encoding(raw)
    text = raw.decode(codec, errors="replace")
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            m = HDR_RE.match(s[1:])
            if m and m.group(1).upper() == "TITLE":
                return m.group(2).strip()
    return ""


# --------------------------------------------------------------------------
# 3. Library scanning
# --------------------------------------------------------------------------

def chart_matches(chart: Chart, needle: str) -> bool:
    hay = " > ".join(chart.categories + (chart.folder_name, chart.path.name,
                                         chart.title))
    return needle.casefold() in hay.casefold()


def scan_library(songs_dir: Path, progress=None) -> list:
    charts: list[Chart] = []
    visited = {"n": 0}

    def walk(d: Path, cats: tuple, is_root: bool):
        visited["n"] += 1
        if progress and visited["n"] % 25 == 0:
            progress(visited["n"])
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            return

        dirs = sorted((e for e in entries
                       if e.is_dir() and not e.name.startswith(".")),
                      key=lambda e: natural_key(e.name))
        files = [e for e in entries if e.is_file()]

        box = next((e for e in files if e.name.casefold() == "box.def"), None)
        if box is not None and not is_root:
            title = read_box_title(Path(box.path))
            cats = cats + (title or Path(box.path).parent.name,)

        tjas = sorted((e for e in files if e.name.casefold().endswith(".tja")),
                      key=lambda e: natural_key(e.name))
        for e in tjas:
            p = Path(e.path)
            try:
                charts.append(load_chart(p, p.parent, cats))
            except Exception as ex:
                err = Chart(p, p.parent, cats, "utf-8", "\n", False, [], 0,
                            "", "", "", "", error=f"{type(ex).__name__}: {ex}")
                charts.append(err)
        for e in dirs:
            walk(Path(e.path), cats, False)

    walk(Path(songs_dir), (), True)
    for i, c in enumerate(charts):
        c.iid = f"S{i}"
    return charts


# --------------------------------------------------------------------------
# 4. External tools (yt-dlp / ffmpeg)
# --------------------------------------------------------------------------

@dataclass
class Tool:
    cmd: list | None
    where: str

    def ok(self) -> bool:
        return bool(self.cmd)


def resolve_ytdlp(cfg_path: str) -> Tool:
    if cfg_path:
        p = Path(cfg_path)
        if p.is_file():
            return Tool([str(p)], "settings")
    for name in ("yt-dlp.exe", "yt-dlp"):
        p = SCRIPT_DIR / name
        if p.is_file():
            return Tool([str(p)], "next to script")
    w = shutil.which("yt-dlp")
    if w:
        return Tool([w], "PATH")
    try:
        if importlib.util.find_spec("yt_dlp"):
            return Tool([sys.executable, "-m", "yt_dlp"], "python -m yt_dlp")
    except Exception:
        pass
    return Tool(None, "not found")


def resolve_ffmpeg(cfg_path: str) -> Tool:
    if cfg_path:
        p = Path(cfg_path)
        if p.is_file():
            return Tool([str(p)], "settings")
    for name in ("ffmpeg.exe", "ffmpeg"):
        p = SCRIPT_DIR / name
        if p.is_file():
            return Tool([str(p)], "next to script")
    w = shutil.which("ffmpeg")
    if w:
        return Tool([w], "PATH")
    return Tool(None, "not found")


def build_search_cmd(tool: Tool, query: str, count: int) -> list:
    return list(tool.cmd) + [
        "--flat-playlist", "--no-warnings", "--socket-timeout", "30",
        "-J", f"ytsearch{count}:{query}",
    ]


def build_download_cmd(tool: Tool, ffmpeg_tool: Tool, url: str,
                       out_dir: Path, out_stem: str, max_res) -> list:
    # '-t mp4' (yt-dlp preset) ==  --merge-output-format mp4 --remux-video mp4
    #                              -S vcodec:h264,...,acodec:aac
    # Additional '-S res:N' is prepended to the sort, capping resolution.
    cmd = list(tool.cmd) + [
        "--no-playlist", "--newline", "--socket-timeout", "30",
        "-t", "mp4",
    ]
    if max_res:
        cmd += ["-S", f"res:{max_res}"]
    cmd += [
        "--downloader", "ffmpeg",
        "--downloader-args", "ffmpeg_i:-nostdin -hide_banner -loglevel warning -stats -y",
    ]
    if ffmpeg_tool.ok():
        cmd += ["--ffmpeg-location", str(Path(ffmpeg_tool.cmd[0]).parent)]
    cmd += ["-o", str(Path(out_dir) / (out_stem + ".%(ext)s")), "--", url]
    return cmd


def run_search(tool: Tool, query: str, count: int, timeout: int = 90) -> list:
    if not tool.ok():
        raise ToolError("yt-dlp was not found")
    cmd = build_search_cmd(tool, query, count)
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=CREATE_NO_WINDOW, timeout=timeout)
    except FileNotFoundError as e:
        raise ToolError(f"Could not start yt-dlp: {e}")
    except subprocess.TimeoutExpired:
        raise ToolError("YouTube search timed out")

    results = []
    out = p.stdout or ""
    idx = out.find("{")
    data = None
    if idx >= 0:
        try:
            data = json.loads(out[idx:])
        except json.JSONDecodeError:
            data = None
    if isinstance(data, dict):
        for entry in data.get("entries") or []:
            vid = entry.get("id")
            if not vid:
                continue
            results.append({
                "id": vid,
                "url": entry.get("webpage_url")
                       or f"https://www.youtube.com/watch?v={vid}",
                "title": entry.get("title") or "(no title)",
                "channel": entry.get("uploader") or entry.get("channel") or "",
                "duration": entry.get("duration"),
                "views": entry.get("view_count"),
            })
    if not results:
        err = (p.stderr or "").strip() or out.strip()
        err = err[-800:]
        raise ToolError(err or f"yt-dlp returned no results (exit {p.returncode})")
    return results


def fmt_duration(sec) -> str:
    if sec is None:
        return "live/?"
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class LineReader:
    """Run a subprocess and iterate its output, splitting on \\r and \\n
    (ffmpeg's -stats progress lines are \\r separated)."""

    def __init__(self, cmd: list):
        self.p = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", creationflags=CREATE_NO_WINDOW)
        self._buf = ""

    def lines(self):
        stream = self.p.stdout
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            self._buf += chunk
            while True:
                m = re.search(r"[\r\n]", self._buf)
                if not m:
                    break
                line = self._buf[:m.start()]
                self._buf = self._buf[m.end():]
                if line.strip():
                    yield line
        if self._buf.strip():
            yield self._buf
            self._buf = ""

    def terminate(self):
        try:
            self.p.terminate()
        except Exception:
            pass

    def wait(self) -> int:
        try:
            return self.p.wait(timeout=30)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass
            try:
                return self.p.wait(timeout=10)
            except Exception:
                return -1


# --------------------------------------------------------------------------
# 5. Configuration
# --------------------------------------------------------------------------

CONFIG_DEFAULTS = {
    "songs_dir": "",
    "ytdlp_path": "",
    "ffmpeg_path": "",
    "auto_pick": False,
    "max_res": "1080p (recommended)",
    "geometry": "",
}


def load_config() -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def default_songs_dir(cfg: dict) -> Path | None:
    p = cfg.get("songs_dir") or ""
    if p and Path(p).is_dir():
        return Path(p)
    for base in [SCRIPT_DIR, *SCRIPT_DIR.parents[:3]]:
        for cand in (base / "Songs",
                     base / "OpenTaiko" / "Songs",
                     base / "OpenTaiko" / "OpenTaiko" / "Songs"):
            if cand.is_dir():
                return cand
    return None


# --------------------------------------------------------------------------
# 6. GUI — picker dialog
# --------------------------------------------------------------------------

class PickerDialog(tk.Toplevel):
    """Per-song dialog: pick a YouTube result, paste a URL, set an optional
    MOVIEOFFSET, skip, or cancel the whole batch."""

    def __init__(self, master, chart: Chart, results, search_err,
                 query: str, tool: Tool, deliver):
        super().__init__(master)
        self.chart = chart
        self.tool = tool
        self.deliver = deliver
        self._results = []
        self._q = queue.Queue()
        self.result_sent = False

        self.title("Choose a video — " + (chart.title or chart.path.stem))
        self.minsize(780, 480)
        self.transient(master)

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=chart.title or "(untitled)",
                  font=("", 11, "bold")).pack(anchor="w")
        cat = " > ".join(chart.categories) if chart.categories else "—"
        ttk.Label(frm, foreground="#666666",
                  text=f'Category: {cat}    Chart: {chart.path.name}'
                       f'    Folder: {chart.folder_name}').pack(anchor="w")

        # --- search row -------------------------------------------------
        srow = ttk.Frame(frm)
        srow.pack(fill="x", pady=(10, 2))
        ttk.Label(srow, text="Search query:").pack(side="left")
        self.query_var = tk.StringVar(value=query)
        qe = ttk.Entry(srow, textvariable=self.query_var)
        qe.pack(side="left", fill="x", expand=True, padx=6)
        qe.bind("<Return>", lambda _e: self._do_search())
        self.search_btn = ttk.Button(srow, text="Search again",
                                     command=self._do_search)
        self.search_btn.pack(side="left")

        self.status_lbl = ttk.Label(frm, foreground="#666666")
        self.status_lbl.pack(anchor="w", pady=(0, 4))

        # --- results -----------------------------------------------------
        cols = ("title", "channel", "duration")
        tvf = ttk.Frame(frm)
        tvf.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(tvf, columns=cols, show="headings",
                               height=8, selectmode="browse")
        self.tv.heading("title", text="Title")
        self.tv.column("title", width=430, anchor="w")
        self.tv.heading("channel", text="Channel")
        self.tv.column("channel", width=190, anchor="w")
        self.tv.heading("duration", text="Duration")
        self.tv.column("duration", width=90, anchor="center", stretch=False)
        vsb = ttk.Scrollbar(tvf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=vsb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tv.bind("<Double-1>", lambda _e: self._finish("download"))

        # --- URL row -----------------------------------------------------
        urow = ttk.Frame(frm)
        urow.pack(fill="x", pady=(8, 0))
        ttk.Label(urow, text="or paste a YouTube URL:").pack(side="left")
        self.url_var = tk.StringVar()
        try:
            clip = master.clipboard_get()
            if re.match(r"\s*https?://", clip):
                self.url_var.set(clip.strip())
        except tk.TclError:
            pass
        ttk.Entry(urow, textvariable=self.url_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(urow, text="Use this URL",
                   command=self._use_url).pack(side="left")

        # --- offset row ----------------------------------------------------
        orow = ttk.Frame(frm)
        orow.pack(fill="x", pady=(8, 0))
        ttk.Label(orow, text="MOVIEOFFSET (seconds, optional — delays the "
                             "video start; blank keeps the current value):"
                  ).pack(side="left")
        self.offset_var = tk.StringVar(value=chart.movieoffset or "")
        ttk.Entry(orow, textvariable=self.offset_var, width=10).pack(
            side="left", padx=6)

        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, variable=self.auto_var,
                        text="Auto-pick the first result for the remaining "
                             "songs (no more dialogs)"
                        ).pack(anchor="w", pady=(8, 4))

        # --- buttons -------------------------------------------------------
        brow = ttk.Frame(frm)
        brow.pack(fill="x", pady=(4, 0))
        ttk.Button(brow, text="Download selected",
                   command=lambda: self._finish("download")).pack(
            side="right", padx=(6, 0))
        ttk.Button(brow, text="Skip this song",
                   command=lambda: self._finish("skip")).pack(side="right")
        ttk.Button(brow, text="Cancel batch",
                   command=lambda: self._finish("cancel")).pack(
            side="right", padx=(0, 6))

        self.protocol("WM_DELETE_WINDOW", lambda: self._finish("skip"))
        self._set_results(results, search_err)
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.after(120, self._poll)
        self._center(master)

    # ------------------------------------------------------------------
    def _center(self, master):
        try:
            self.update_idletasks()
            mx, my = master.winfo_x(), master.winfo_y()
            mw, mh = master.winfo_width(), master.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{mx + (mw - w) // 2}+{my + (mh - h) // 3}")
        except Exception:
            pass

    def _poll(self):
        try:
            while True:
                kind, res, err = self._q.get_nowait()
                if kind == "ok":
                    self._set_results(res, None)
                else:
                    self._set_results([], err)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

    def _set_results(self, results, err):
        self._results = results or []
        self.tv.delete(*self.tv.get_children())
        for i, r in enumerate(self._results):
            self.tv.insert("", "end", iid=str(i), values=(
                r.get("title", ""), r.get("channel", ""),
                fmt_duration(r.get("duration"))))
        self.search_btn.configure(state="normal")
        if err:
            self.status_lbl.configure(foreground="#cf222e")
            self.status_lbl.configure(text="Search failed: " + err)
        elif not self._results:
            self.status_lbl.configure(foreground="#cf222e")
            self.status_lbl.configure(text="No results found — try another "
                                           "query or paste a URL below.")
        else:
            self.tv.selection_set("0")
            self.tv.focus("0")
            self.status_lbl.configure(foreground="#1a7f37")
            self.status_lbl.configure(
                text=f"{len(self._results)} results — double-click a row to "
                     f"download it.")

    def _do_search(self):
        q = self.query_var.get().strip()
        if not q or not self.tool.ok():
            return
        self.search_btn.configure(state="disabled")
        self.status_lbl.configure(foreground="#666666")
        self.status_lbl.configure(text="Searching…")

        def work():
            try:
                res = run_search(self.tool, q, SEARCH_RESULTS)
                self._q.put(("ok", res, None))
            except ToolError as e:
                self._q.put(("err", None, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _use_url(self):
        u = self.url_var.get().strip()
        if not u:
            self.status_lbl.configure(foreground="#cf222e")
            self.status_lbl.configure(text="Paste a URL first.")
            return
        self._finish("download", url=u)

    def _finish(self, kind, url=None):
        if self.result_sent:
            return
        if kind == "download":
            if not url:
                sel = self.tv.selection()
                if not sel:
                    self.status_lbl.configure(foreground="#cf222e")
                    self.status_lbl.configure(text="Select a result first.")
                    return
                url = self._results[int(sel[0])]["url"]
            offset = self.offset_var.get().strip()
            if offset:
                try:
                    float(offset)
                except ValueError:
                    self.status_lbl.configure(foreground="#cf222e")
                    self.status_lbl.configure(
                        text="MOVIEOFFSET must be a number, e.g. -1.5 or 2.")
                    return
            self.result_sent = True
            self.deliver({"kind": "download", "url": url,
                          "offset": offset or None,
                          "auto": bool(self.auto_var.get())})
        else:
            self.result_sent = True
            self.deliver({"kind": kind, "url": None, "offset": None,
                          "auto": bool(self.auto_var.get())})
        self.destroy()


# --------------------------------------------------------------------------
# 7. GUI — main window
# --------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.charts: list[Chart] = []
        self.iid_map: dict[str, Chart] = {}
        self._open_cats: set[str] = set()
        self._open_all = False
        self.ui_queue: queue.Queue = queue.Queue()
        self.tool_ytdlp = Tool(None, "")
        self.tool_ffmpeg = Tool(None, "")
        self.worker: DownloadWorker | None = None
        self.stop_event = threading.Event()
        self.scanning = False
        self._log_count = 0

        root.title(APP_TITLE)
        root.geometry(self.cfg.get("geometry") or "1150x720")
        root.minsize(950, 620)

        self._build_style()
        self._build_ui()

        self._resolve_tools(log=False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._pump)
        root.after(60, self.start_scan)

    # ------------------------------------------------------------------ UI
    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", rowheight=24)
        self.bold_font = tkfont.nametofont("TkDefaultFont").copy()
        self.bold_font.configure(weight="bold")

    def _build_ui(self):
        root = self.root

        # ---- row 0: songs folder ------------------------------------
        top = ttk.Frame(root, padding=(10, 8))
        top.pack(fill="x")
        r0 = ttk.Frame(top)
        r0.pack(fill="x", pady=2)
        ttk.Label(r0, text="Songs folder:").pack(side="left")
        self.songs_var = tk.StringVar()
        e = ttk.Entry(r0, textvariable=self.songs_var)
        e.pack(side="left", fill="x", expand=True, padx=6)
        e.bind("<Return>", lambda _ev: self.start_scan())
        ttk.Button(r0, text="Browse…", command=self._browse_songs).pack(
            side="left", padx=2)
        self.rescan_btn = ttk.Button(r0, text="Rescan",
                                     command=self.start_scan)
        self.rescan_btn.pack(side="left", padx=2)

        # ---- row 1: tools ---------------------------------------------
        r1 = ttk.Frame(top)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="yt-dlp:").pack(side="left")
        self.ytdlp_var = tk.StringVar(value=self.cfg.get("ytdlp_path", ""))
        ttk.Entry(r1, textvariable=self.ytdlp_var, width=42).pack(
            side="left", padx=(4, 2))
        ttk.Button(r1, text="…", width=3,
                   command=lambda: self._browse_tool("yt-dlp")).pack(
            side="left", padx=(0, 12))
        ttk.Label(r1, text="ffmpeg:").pack(side="left")
        self.ffmpeg_var = tk.StringVar(value=self.cfg.get("ffmpeg_path", ""))
        ttk.Entry(r1, textvariable=self.ffmpeg_var, width=42).pack(
            side="left", padx=(4, 2))
        ttk.Button(r1, text="…", width=3,
                   command=lambda: self._browse_tool("ffmpeg")).pack(side="left")
        self.tools_status_lbl = ttk.Label(r1)
        self.tools_status_lbl.pack(side="left", padx=12)

        # ---- row 2: filter + selection helpers ------------------------
        r2 = ttk.Frame(top)
        r2.pack(fill="x", pady=(6, 2))
        ttk.Label(r2, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        fe = ttk.Entry(r2, textvariable=self.filter_var, width=32)
        fe.pack(side="left", padx=6)
        self.filter_var.trace_add("write", lambda *_a: self.rebuild_tree())
        ttk.Button(r2, text="Select all shown",
                   command=lambda: self._select_visible(True)).pack(
            side="left", padx=(12, 2))
        ttk.Button(r2, text="Select none",
                   command=lambda: self._select_visible(False)).pack(
            side="left", padx=2)
        ttk.Button(r2, text="Select shown without video",
                   command=lambda: self._select_visible(True,
                                                        only_no_video=True)
                   ).pack(side="left", padx=2)
        self.auto_pick_var = tk.BooleanVar(
            value=bool(self.cfg.get("auto_pick", False)))
        ttk.Checkbutton(r2, variable=self.auto_pick_var,
                        text="Auto-pick first result (no dialogs)").pack(
            side="left", padx=(16, 0))
        ttk.Label(r2, text="Max resolution:").pack(side="left", padx=(16, 2))
        self._max_res_map = {label: val for label, val in MAX_RES_CHOICES}
        self.max_res_var = tk.StringVar(
            value=self.cfg.get("max_res", "1080p (recommended)"))
        if self.max_res_var.get() not in self._max_res_map:
            self.max_res_var.set("1080p (recommended)")
        ttk.Combobox(r2, textvariable=self.max_res_var, state="readonly",
                     width=20, values=list(self._max_res_map)).pack(side="left")

        # ---- tree --------------------------------------------------------
        mid = ttk.Frame(root)
        mid.pack(fill="both", expand=True, padx=10, pady=(2, 4))
        cols = ("sel", "title", "video", "chart")
        self.tree = ttk.Treeview(mid, columns=cols, show="tree headings",
                                 selectmode="extended")
        self.tree.heading("#0", text="Folder / category")
        self.tree.column("#0", width=280, anchor="w", stretch=True)
        self.tree.heading("sel", text="✓")
        self.tree.column("sel", width=46, anchor="center", stretch=False)
        self.tree.heading("title", text="Song title")
        self.tree.column("title", width=330, anchor="w", stretch=True)
        self.tree.heading("video", text="Video")
        self.tree.column("video", width=230, anchor="w", stretch=True)
        self.tree.heading("chart", text="Chart file")
        self.tree.column("chart", width=200, anchor="w", stretch=True)
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree.tag_configure("ok", foreground="#1a7f37")
        self.tree.tag_configure("none", foreground="#6e7781")
        self.tree.tag_configure("miss", foreground="#cf222e")
        self.tree.tag_configure("err", foreground="#b00020")
        self.tree.tag_configure("cat", font=self.bold_font)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._on_tree_space)
        self.tree.bind("<<TreeviewOpen>>", self._on_cat_open)
        self.tree.bind("<<TreeviewClose>>", self._on_cat_close)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self._ctx_menu = tk.Menu(root, tearoff=0)
        self._ctx_menu.add_command(label="Open song folder",
                                   command=self._open_song_folder)

        # ---- progress + log -------------------------------------------
        pb = ttk.Frame(root)
        pb.pack(fill="x", padx=10)
        self.progress_lbl = ttk.Label(pb, text="", foreground="#444444")
        self.progress_lbl.pack(anchor="w")
        self.pbar = ttk.Progressbar(pb, mode="determinate")
        self.pbar.pack(fill="x", pady=(0, 4))

        logf = ttk.Frame(root)
        logf.pack(fill="both", padx=10, pady=(0, 4))
        self.log_text = tk.Text(logf, height=9, state="disabled",
                                wrap="none", font=("Consolas", 9))
        lsb = ttk.Scrollbar(logf, orient="vertical",
                            command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")
        for tag, color in (("info", "#24292f"), ("ok", "#1a7f37"),
                           ("err", "#cf222e"), ("warn", "#9a6700"),
                           ("dim", "#6e7781")):
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.tag_configure("head", foreground="#24292f",
                                    font=("Consolas", 9, "bold"))

        # ---- bottom bar -------------------------------------------------
        bot = ttk.Frame(root, padding=(10, 0, 10, 8))
        bot.pack(fill="x")
        self.download_btn = ttk.Button(
            bot, text="Download videos for 0 selected songs",
            command=self.start_batch)
        self.download_btn.pack(side="left")
        self.stop_btn = ttk.Button(bot, text="Stop", command=self.stop_batch,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self.status_var, foreground="#555555"
                  ).pack(side="right")

        d = default_songs_dir(self.cfg)
        if d:
            self.songs_var.set(str(d))

    # ------------------------------------------------------------- helpers
    def ui(self, fn):
        self.ui_queue.put(fn)

    def _pump(self):
        try:
            # Process at most 100 queued tasks per tick so that the Tcl
            # event loop keeps servicing window redraws, clicks and drags.
            # Draining the whole queue in one pass would freeze the UI
            # during yt-dlp's high-volume status output.
            for _ in range(100):
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def log(self, msg, kind="info"):
        self.ui(lambda: self._log_line(msg, kind))

    def _log_line(self, msg, kind="info"):
        t = self.log_text
        t.configure(state="normal")
        t.insert("end", msg + "\n", kind)
        self._log_count += 1
        if self._log_count > 3000:
            t.delete("1.0", "1000.0")
            self._log_count -= 1000
        t.see("end")
        t.configure(state="disabled")

    def set_progress(self, text):
        self.ui(lambda: self.progress_lbl.configure(
            text=(text or "")[-170:]))

    def set_batch_progress(self, done, total):
        def apply():
            self.pbar.configure(maximum=max(total, 1), value=done)
        self.ui(apply)

    # -------------------------------------------------------------- tools
    def _resolve_tools(self, log=True):
        self.cfg["ytdlp_path"] = self.ytdlp_var.get().strip()
        self.cfg["ffmpeg_path"] = self.ffmpeg_var.get().strip()
        self.tool_ytdlp = resolve_ytdlp(self.cfg["ytdlp_path"])
        self.tool_ffmpeg = resolve_ffmpeg(self.cfg["ffmpeg_path"])
        if self.tool_ytdlp.ok():
            ytxt = f'yt-dlp ✓ ({self.tool_ytdlp.where})'
        else:
            ytxt = "yt-dlp ✗ NOT FOUND"
        if self.tool_ffmpeg.ok():
            ftxt = f'ffmpeg ✓ ({self.tool_ffmpeg.where})'
        else:
            ftxt = "ffmpeg ✗ NOT FOUND"
        missing = not (self.tool_ytdlp.ok() and self.tool_ffmpeg.ok())
        self.tools_status_lbl.configure(
            text=f"{ytxt}   •   {ftxt}",
            foreground="#cf222e" if missing else "#1a7f37")
        if log:
            self.log(
                f"Tools: yt-dlp={self.tool_ytdlp.cmd} "
                f"({self.tool_ytdlp.where}), "
                f"ffmpeg={self.tool_ffmpeg.cmd} "
                f"({self.tool_ffmpeg.where})", "dim")

    def _browse_songs(self):
        d = filedialog.askdirectory(title="Select the OpenTaiko Songs folder")
        if d:
            self.songs_var.set(d)
            self.cfg["songs_dir"] = d
            save_config(self.cfg)
            self.start_scan()

    def _browse_tool(self, which: str):
        f = filedialog.askopenfilename(
            title=f"Select the {which} executable",
            filetypes=(("Executable", "*.exe"), ("All files", "*.*")))
        if f:
            if which == "yt-dlp":
                self.ytdlp_var.set(f)
            else:
                self.ffmpeg_var.set(f)
            self._resolve_tools(log=True)
            save_config(self.cfg)

    # -------------------------------------------------------------- scan
    def start_scan(self):
        if self.scanning:
            return
        d = self.songs_var.get().strip()
        if not d or not Path(d).is_dir():
            self.log('Songs folder is not set — use "Browse…" to pick your '
                     'OpenTaiko Songs folder.', "warn")
            return
        self.scanning = True
        self.cfg["songs_dir"] = d
        save_config(self.cfg)
        self.rescan_btn.configure(state="disabled")
        self.log(f"Scanning {d} …", "dim")
        self.set_progress("Scanning…")

        def work():
            try:
                charts = scan_library(
                    Path(d),
                    progress=lambda n: self.set_progress(f"Scanning… {n} folders"))
            except Exception as ex:
                self.log(f"Scan failed: {ex}", "err")
                charts = []
            self.ui(lambda: self._apply_scan(charts))

        threading.Thread(target=work, daemon=True).start()

    def _apply_scan(self, charts):
        self.scanning = False
        self.charts = charts
        self.iid_map = {c.iid: c for c in charts}
        ok = miss = none_v = err = 0
        for c in charts:
            s = c.video_state()[0]
            if s == "ok":
                ok += 1
            elif s == "miss":
                miss += 1
            elif s == "err":
                err += 1
            else:
                none_v += 1
        self.log(f"Found {len(charts)} charts in "
                 f'"{self.songs_var.get()}" — {ok} with video, {miss} '
                 f"referencing a missing file, {none_v} without video, "
                 f"{err} unreadable.", "ok")
        if err:
            for c in charts:
                if c.error:
                    self.log(f"  ✗ {c.path}: {c.error}", "warn")
        self._open_all = True
        self._open_cats = set()
        self.rebuild_tree()
        self.rescan_btn.configure(state="normal")

    def rebuild_tree(self):
        tv = self.tree
        tv.delete(*tv.get_children(""))
        needle = self.filter_var.get().strip()
        groups: dict = {}
        for c in self.charts:
            groups.setdefault(c.categories, []).append(c)
        shown_total = 0
        for chain, group in groups.items():
            shown = [c for c in group if not needle or chart_matches(c, needle)]
            if not shown:
                continue
            cat_iid = "C" + "\x1f".join(chain)
            cat_name = chain[-1] if chain else "(no category)"
            k = sum(1 for c in shown if c.selected)
            if self._open_all:
                self._open_cats.add(cat_iid)
            tv.insert("", "end", iid=cat_iid, text=cat_name,
                      values=(f"{k}/{len(shown)}", "", "", ""),
                      open=(cat_iid in self._open_cats), tags=("cat",))
            for c in shown:
                tag, text = c.video_state()
                glyph = "\u2611" if c.selected else "\u2610"
                tv.insert(cat_iid, "end", iid=c.iid, text=c.folder_name,
                          values=(glyph, c.title or "(untitled)", text,
                                  c.path.name), tags=(tag,))
                shown_total += 1
        if not shown_total:
            tv.insert("", "end", text="(no charts found)",
                      values=("", "", "", ""), tags=("none",))
        self._update_counts()

    # ---------------------------------------------------------- selection
    def _on_tree_click(self, ev):
        iid = self.tree.identify_row(ev.y)
        if iid in self.iid_map:
            self._toggle_chart(iid)

    def _on_tree_space(self, _ev):
        for iid in self.tree.selection():
            if iid in self.iid_map:
                self._toggle_chart(iid)
        return "break"

    def _toggle_chart(self, iid):
        c = self.iid_map.get(iid)
        if not c:
            return
        c.selected = not c.selected
        self.tree.set(iid, "sel", "\u2611" if c.selected else "\u2610")
        parent = self.tree.parent(iid)
        if parent and self.tree.exists(parent):
            kids = self.tree.get_children(parent)
            k = sum(1 for kk in kids
                    if self.iid_map.get(kk) and self.iid_map[kk].selected)
            self.tree.set(parent, "sel", f"{k}/{len(kids)}")
        self._update_counts()

    def _select_visible(self, value: bool, only_no_video: bool = False):
        needle = self.filter_var.get().strip()
        for c in self.charts:
            if needle and not chart_matches(c, needle):
                continue
            if only_no_video and c.video_state()[0] in ("ok", "err"):
                continue
            c.selected = value
        self.rebuild_tree()

    def _update_counts(self):
        total = ok = miss = none_v = err = sel = 0
        for c in self.charts:
            s = c.video_state()[0]
            if s == "ok":
                ok += 1
            elif s == "miss":
                miss += 1
            elif s == "err":
                err += 1
            else:
                none_v += 1
            if c.selected:
                sel += 1
        self.status_var.set(
            f"{total} charts  •  {ok} with video  •  {miss} missing file  •  "
            f"{none_v} without video  •  {sel} selected")
        self.download_btn.configure(
            text=f"Download videos for {sel} selected songs",
            state=("normal" if sel and not self.worker else "disabled"))

    # -------------------------------------------------------------- misc UI
    def _on_cat_open(self, _ev):
        self._open_all = False
        sel = self.tree.focus()
        if sel:
            self._open_cats.add(sel)

    def _on_cat_close(self, _ev):
        self._open_all = False
        sel = self.tree.focus()
        self._open_cats.discard(sel)

    def _on_tree_right_click(self, ev):
        iid = self.tree.identify_row(ev.y)
        if iid in self.iid_map:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            try:
                self._ctx_menu.tk_popup(ev.x_root, ev.y_root)
            finally:
                self._ctx_menu.grab_release()

    def _open_song_folder(self):
        sel = self.tree.selection()
        if not sel:
            return
        c = self.iid_map.get(sel[0])
        if not c:
            return
        try:
            if os.name == "nt":
                os.startfile(str(c.song_dir))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(c.song_dir)])
            else:
                subprocess.Popen(["xdg-open", str(c.song_dir)])
        except Exception as ex:
            self.log(f"Could not open folder: {ex}", "err")

    def refresh_chart_row(self, iid):
        c = self.iid_map.get(iid)
        if not c:
            return
        try:
            fresh = load_chart(c.path, c.song_dir, c.categories)
        except Exception:
            fresh = c
        fresh.iid = c.iid
        fresh.selected = c.selected
        for i, ch in enumerate(self.charts):
            if ch.iid == iid:
                self.charts[i] = fresh
                break
        self.iid_map[iid] = fresh
        if self.tree.exists(iid):
            tag, text = fresh.video_state()
            self.tree.set(iid, "video", text)
            self.tree.item(iid, tags=(tag,))
            self.tree.set(iid, "sel",
                          "\u2611" if fresh.selected else "\u2610")
        self._update_counts()

    def ask_pick(self, chart, results, search_err, query):
        """Called from the worker thread: show the picker dialog on the UI
        thread and block until the user answers."""
        resp = {"r": None}
        ev = threading.Event()

        def show():
            def deliver(result):
                resp["r"] = result or {"kind": "skip"}
                ev.set()
            try:
                PickerDialog(self.root, chart, results, search_err, query,
                             self.tool_ytdlp, deliver)
            except Exception:
                self._log_line(
                    "Could not show picker dialog:\n"
                    + traceback.format_exc(), "err")
                resp["r"] = {"kind": "skip"}
                ev.set()

        self.ui(show)
        while not ev.wait(0.5):
            if self.stop_event.is_set():
                return {"kind": "cancel"}
            try:
                if not self.root.winfo_exists():
                    return {"kind": "cancel"}
            except Exception:
                return {"kind": "cancel"}

        # Let the UI thread fully destroy the dialog and release its grab
        # before the worker moves on to the next song.
        time.sleep(0.2)
        return resp["r"]

    # -------------------------------------------------------------- batch
    def start_batch(self):
        if self.worker:
            return
        sel = [c for c in self.charts if c.selected]
        if not sel:
            return
        if not self.tool_ytdlp.ok():
            messagebox.showerror(
                APP_TITLE, "yt-dlp was not found.\n\nDownload yt-dlp.exe from "
                "https://github.com/yt-dlp/yt-dlp/releases and place it next "
                "to this script (or on PATH), or set its location above.")
            return
        if not self.tool_ffmpeg.ok():
            messagebox.showerror(
                APP_TITLE, "ffmpeg was not found.\n\nInstall ffmpeg and put "
                "it on PATH (or next to this script), or set its location "
                "above.")
            return
        self.stop_event.clear()
        auto = bool(self.auto_pick_var.get())
        max_res = self._max_res_map.get(self.max_res_var.get(), 1080)
        self.worker = DownloadWorker(self, sel, auto_pick=auto,
                                     max_res=max_res)
        self.worker.start()
        self.download_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.rescan_btn.configure(state="disabled")

    def stop_batch(self):
        if not self.worker:
            return
        self.stop_event.set()
        self.worker.terminate_current()
        self.log("Stopping…", "warn")

    def batch_finished(self):
        self.worker = None
        self.download_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.rescan_btn.configure(state="normal")
        self._update_counts()

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                    APP_TITLE, "Downloads are still running.\nStop them and "
                               "quit?"):
                return
            self.stop_event.set()
            self.worker.terminate_current()
            self.worker.join(timeout=5)
        self.cfg["geometry"] = self.root.winfo_geometry()
        self.cfg["auto_pick"] = bool(self.auto_pick_var.get())
        self.cfg["max_res"] = self.max_res_var.get()
        save_config(self.cfg)
        self.root.destroy()


# --------------------------------------------------------------------------
# 8. Download worker
# --------------------------------------------------------------------------

PROGRESS_RE = re.compile(r"\[download\]\s+\d+(?:\.\d+)?%")


class DownloadWorker(threading.Thread):
    def __init__(self, app: App, charts, auto_pick: bool, max_res):
        super().__init__(daemon=True, name="DownloadWorker")
        self.app = app
        self.charts = charts
        self.auto_pick = auto_pick
        self.max_res = max_res
        self._proc = None
        self._proc_lock = threading.Lock()

    def terminate_current(self):
        with self._proc_lock:
            if self._proc is not None:
                self._proc.terminate()

    def run(self):
        total = len(self.charts)
        ok = failed = skipped = 0
        self.app.log(f"Starting downloads for {total} chart(s).", "head")
        try:
            for i, c in enumerate(self.charts):
                if self.app.stop_event.is_set():
                    break
                self.app.set_batch_progress(i, total)
                try:
                    res = self._process(c, i, total)
                except ToolError as e:
                    failed += 1
                    self.app.log(f"  ✗ {e}", "err")
                except Exception:
                    failed += 1
                    self.app.log("  ✗ unexpected error:\n"
                                 + traceback.format_exc(limit=3), "err")
                else:
                    if res == "ok":
                        ok += 1
                    elif res == "skip":
                        skipped += 1
                    elif res == "cancel":
                        break
        finally:
            stopped = self.app.stop_event.is_set()
            self.app.set_batch_progress(total, total)
            summary = (f"Done — {ok} downloaded, {failed} failed, "
                       f"{skipped} skipped.")
            if stopped:
                summary += " Stopped by user."
            self.app.log(summary, "warn" if stopped else "head")
            self.app.set_progress("")
            self.app.ui(self.app.batch_finished)

    def _process(self, c: Chart, i: int, total: int) -> str:
        app = self.app
        tag = f"[{i + 1}/{total}]"
        title = c.title or c.path.stem
        app.log(f"{tag} {title}", "head")
        app.log(f"      {c.path}", "dim")

        # -- 1. search ---------------------------------------------------
        query = (c.title or c.folder_name).strip()
        app.set_progress(f'{tag} Searching YouTube for "{query}" …')
        results, search_err = [], None
        try:
            results = run_search(app.tool_ytdlp, query, SEARCH_RESULTS)
        except ToolError as e:
            search_err = str(e)
            app.log(f"  search failed: {search_err}", "err")
        if app.stop_event.is_set():
            return "cancel"

        # -- 2. let the user pick (unless auto mode) -----------------------
        if self.auto_pick:
            if not results:
                app.log("  ✗ skipped: no search results (auto mode)", "err")
                return "fail"
            choice = {"kind": "download", "url": results[0]["url"],
                      "offset": None, "auto": False}
            app.log(f'  auto-picked: {results[0]["title"]}', "dim")
        else:
            choice = app.ask_pick(c, results, search_err, query)
            kind = choice.get("kind")
            if kind == "cancel":
                app.stop_event.set()
                return "cancel"
            if kind == "skip":
                app.log("  — skipped by user", "dim")
                return "skip"
            if choice.get("auto"):
                self.auto_pick = True
                app.log("  auto-pick enabled for the remaining songs", "dim")

        url = (choice.get("url") or "").strip()
        offset = choice.get("offset")
        if not url:
            app.log("  ✗ no URL chosen", "err")
            return "fail"

        # -- 3. download ----------------------------------------------------
        video_name = proposed_video_name(c)
        video_stem = Path(video_name).stem
        out_path = c.song_dir / video_name
        app.set_progress(f'{tag} Downloading video for "{title}" …')
        app.log(f"  downloading → {video_name} (ffmpeg downloader)", "dim")
        cmd = build_download_cmd(app.tool_ytdlp, app.tool_ffmpeg, url,
                                 c.song_dir, video_stem, self.max_res)
        app.log("  $ " + " ".join(cmd), "dim")

        proc = LineReader(cmd)
        with self._proc_lock:
            self._proc = proc
        try:
            # Throttle progress (set_progress) updates so a chatty
            # downloader can't flood the UI queue. We only push a progress
            # line to the GUI at most ~10x/second; every other line is a log
            # line and goes through normally.
            last_progress = [0.0]
            for line in proc.lines():
                if app.stop_event.is_set():
                    proc.terminate()
                    break
                ls = line.strip()
                if ls.startswith(("size=", "frame=")) or PROGRESS_RE.match(ls):
                    now = time.monotonic()
                    if now - last_progress[0] >= 0.1:
                        last_progress[0] = now
                        app.set_progress(f"{tag} {ls}")
                else:
                    app.log("  │ " + ls, "dim")
        finally:
            with self._proc_lock:
                self._proc = None
        rc = proc.wait()
        if app.stop_event.is_set():
            app.log("  ✗ download interrupted", "warn")
            return "cancel"
        if rc != 0:
            app.log(f"  ✗ download failed (yt-dlp exited with code {rc})",
                    "err")
            return "fail"

        # -- 4. locate the output file ---------------------------------------
        if not out_path.is_file():
            alts = [p for p in c.song_dir.glob(video_stem + ".*")
                    if p.suffix.casefold() in VIDEO_EXTS and p.is_file()]
            if alts:
                video_name = alts[0].name
                app.log(f"  note: downloaded file is {video_name}", "warn")
            else:
                app.log("  ✗ expected output file not found after download",
                        "err")
                return "fail"

        # -- 5. link it in the .tja -------------------------------------------
        try:
            fresh = load_chart(c.path, c.song_dir, c.categories)
        except Exception as e:
            app.log(f"  ✗ cannot re-read chart: {e}", "err")
            return "fail"
        msg = write_tja_headers(fresh, bgmovie=video_name, movieoffset=offset)
        app.log(f"  ✓ saved {video_name} — {msg}", "ok")
        app.ui(lambda iid=c.iid: app.refresh_chart_row(iid))
        return "ok"


# --------------------------------------------------------------------------
# 9. CLI helpers (scan-only / self test)
# --------------------------------------------------------------------------

def _resolve_songs_dir(arg: str | None, cfg: dict) -> Path | None:
    if arg:
        p = Path(arg)
        if p.is_dir():
            return p
        print(f"ERROR: not a directory: {arg}", file=sys.stderr)
        return None
    d = default_songs_dir(cfg)
    return d


def scan_only(arg=None) -> int:
    cfg = load_config()
    d = _resolve_songs_dir(arg, cfg)
    if not d:
        print("ERROR: could not locate a Songs folder "
              "(pass it as an argument).", file=sys.stderr)
        return 1
    print(f"Scanning {d} …")
    charts = scan_library(d,
                          progress=lambda n: print(f"  … {n} folders",
                                                   end="\r"))
    print()
    cats: dict = {}
    for c in charts:
        cats.setdefault(c.categories, []).append(c)
    for chain, group in cats.items():
        label = " > ".join(chain) if chain else "(no category)"
        print(f"\n== {label} ({len(group)} charts)")
        for c in group:
            tag, text = c.video_state()
            mark = {"ok": "[video ok] ", "miss": "[MISSING] ",
                    "none": "[no video]", "err": "[ERROR]   "}[tag]
            print(f"  {mark} {c.path.name:<30} {c.title or '(untitled)'}")
    print(f"\nTotal: {len(charts)} charts "
          f"({sum(1 for c in charts if c.video_state()[0] == 'ok')} with video)")
    return 0


def self_test() -> int:
    import tempfile

    print("=== OpenTaiko BGMOVIE Downloader self test ===")
    failures = []

    def check(name, cond, extra=""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f" — {extra}" if extra else ""))
        if not cond:
            failures.append(name)

    tools_y = resolve_ytdlp("")
    tools_f = resolve_ffmpeg("")
    check("yt-dlp detected", tools_y.ok(), f"{tools_y.cmd} ({tools_y.where})")
    check("ffmpeg detected", tools_f.ok(), f"{tools_f.cmd} ({tools_f.where})")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        def make(name, text, codec):
            p = td / name
            p.write_bytes(text.encode(codec))
            return p

        utf8 = make("a.tja",
                    "TITLE:Test Song\r\n"
                    "WAVE:a.ogg\r\n"
                    "OFFSET:-0.5\r\n"
                    "\r\n"
                    "COURSE:Oni\r\n"
                    "LEVEL:9\r\n"
                    "#START\r\n"
                    "1011,\r\n"
                    "#END\r\n", "utf-8")
        cp932 = make("b.tja",
                     "TITLE:テストの曲\n"
                     "SUBTITLE:--テスト\n"
                     "WAVE:b.ogg\n"
                     "COURSE:Hard\n"
                     "#START\n"
                     "1111,\n"
                     "#END\n", "cp932")
        u16 = make("c.tja",
                   "TITLE:UTF16 Song\r\nWAVE:c.ogg\r\n"
                   "COURSE:Easy\r\n#START\r\n#END\r\n", "utf-16")

        # --- load + write on UTF-8 chart ---------------------------------
        ch = load_chart(utf8)
        check("utf8: title parsed", ch.title == "Test Song", ch.title)
        check("utf8: newline CRLF", ch.newline == "\r\n")
        check("utf8: no bgmovie initially", ch.bgmovie == "")
        check("utf8: boundary at COURSE", ch.lines[ch.boundary] == "COURSE:Oni")
        msg = write_tja_headers(ch, bgmovie="a.mp4", movieoffset="1.5")
        ch2 = load_chart(utf8)
        check("utf8: BGMOVIE written", ch2.bgmovie == "a.mp4", msg)
        check("utf8: MOVIEOFFSET written", ch2.movieoffset == "1.5")
        check("utf8: title intact", ch2.title == "Test Song")
        check("utf8: inserted in global header",
              any(l == "BGMOVIE:a.mp4" for l in ch2.lines[:ch2.boundary + 1]))
        check("utf8: BGMOVIE before COURSE",
              ch2.lines.index("BGMOVIE:a.mp4") < ch2.lines.index("COURSE:Oni"))
        check("utf8: encoding preserved (no BOM)",
              not utf8.read_bytes().startswith(b"\xef\xbb\xbf"))
        # replace existing
        write_tja_headers(ch2, bgmovie="a2.mp4", movieoffset=None)
        ch3 = load_chart(utf8)
        check("utf8: BGMOVIE replaced", ch3.bgmovie == "a2.mp4")
        check("utf8: old value gone",
              all(l != "BGMOVIE:a.mp4" for l in ch3.lines))

        # --- cp932 chart ---------------------------------------------------
        chj = load_chart(cp932)
        check("cp932: japanese title parsed", chj.title == "テストの曲",
              chj.title)
        check("cp932: codec detected", chj.codec == "cp932")
        check("cp932: newline LF", chj.newline == "\n")
        write_tja_headers(chj, bgmovie="b.mp4")
        raw = cp932.read_bytes()
        chj2 = load_chart(cp932)
        check("cp932: round trip keeps japanese title",
              chj2.title == "テストの曲")
        check("cp932: BGMOVIE written", chj2.bgmovie == "b.mp4")
        check("cp932: still valid cp932", raw.decode("cp932") is not None)
        check("cp932: BGMOVIE before COURSE",
              chj2.lines.index("BGMOVIE:b.mp4") < chj2.lines.index("COURSE:Hard"))

        # --- utf-16 chart ---------------------------------------------------
        chu = load_chart(u16)
        check("utf16: title parsed", chu.title == "UTF16 Song")
        write_tja_headers(chu, bgmovie="c.mp4")
        chu2 = load_chart(u16)
        check("utf16: BGMOVIE written + BOM kept",
              chu2.bgmovie == "c.mp4"
              and u16.read_bytes().startswith(b"\xff\xfe"))

        # --- proposed video name fallback ------------------------------------
        class FakeChart:
            pass

        fc = FakeChart()
        fc.stem = " 日本語の曲 "  # never happens from a real filename, but safe
        fc.codec = "latin-1"
        fc.song_dir = td
        fc.bgmovie = ""
        fc.path = td / "x.tja"
        try:
            name = proposed_video_name(fc)
            check("video name fallback for non-encodable stem",
                  name.endswith(".mp4") and name.isascii(), name)
        except Exception as e:
            check("video name fallback for non-encodable stem", False, str(e))

    # --- library scan ---------------------------------------------------
    d = _resolve_songs_dir(None, load_config())
    if d:
        print(f"\nScanning library at {d} …")
        charts = scan_library(d)
        cats = {}
        for c in charts:
            cats.setdefault(c.categories, []).append(c)
        print(f"  {len(charts)} charts in {len(cats)} categories")
        for chain, group in list(cats.items())[:6]:
            print(f"    {' > '.join(chain) or '(no category)'}: "
                  f"{len(group)} charts")
        check("scan found charts", len(charts) > 0)
        kath = [c for c in charts if c.path.name.lower() == "katharsis.tja"]
        if kath:
            c = kath[0]
            check("scan parses title", c.title.startswith("Katharsis"),
                  c.title)
            check("katharsis has no video yet", c.video_state()[0] != "ok",
                  c.video_state()[1])

    print()
    if failures:
        print(f"SELF TEST FAILED: {failures}")
        return 1
    print("SELF TEST PASSED")
    return 0


# --------------------------------------------------------------------------
# 10. entry point
# --------------------------------------------------------------------------

def main(argv) -> int:
    if "--self-test" in argv:
        return self_test()
    if "--scan-only" in argv or "--scan" in argv:
        i = next((k for k, v in enumerate(argv)
                  if v in ("--scan-only", "--scan")), -1)
        arg = argv[i + 1] if 0 <= i + 1 < len(argv) and not argv[
            i + 1].startswith("-") else None
        return scan_only(arg)
    try:
        root = tk.Tk()
    except Exception as e:
        print(f"ERROR: cannot start the GUI ({e}).\n"
              f"Use --scan-only or --self-test for headless modes.",
              file=sys.stderr)
        return 1
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
