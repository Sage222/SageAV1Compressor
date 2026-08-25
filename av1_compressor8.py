#!/usr/bin/env python3
"""
AV1 Video Compressor
Drag & drop videos to batch re-encode with av1_nvenc for Google Photos.
Preserves original creation_time metadata so Google Photos keeps correct date ordering.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import subprocess
import threading
import time
import os
import re
import json
import queue
import datetime
from pathlib import Path


# ─── Theme ──────────────────────────────────────────────────────────────────

BG         = "#1c1b19"
SURFACE    = "#201f1d"
SURFACE2   = "#2d2c2a"
BORDER     = "#393836"
TEXT       = "#cdccca"
TEXT_MUTED = "#797876"
TEXT_FAINT = "#5a5957"
ACCENT     = "#4f98a3"
ACCENT_DIM = "#31484b"
SUCCESS    = "#6daa45"
WARNING    = "#fdab43"
ERROR      = "#dd6974"
FONT_BODY  = ("Segoe UI", 10)
FONT_MONO  = ("Consolas", 11)
FONT_TITLE = ("Segoe UI Semibold", 11)
FONT_SMALL = ("Segoe UI", 9)

# ── Denoise (CPU) ────────────────────────────────────────────────────────
# hqdn3d is CPU-only but reliable and fast (single spatial+light temporal pass).
DENOISE_FILTERS = {
    "light":  "hqdn3d=1.5:1.5:3:3",
    "medium": "hqdn3d=4:3:6:4.5",
}

DENOISE_BPP_FACTOR = {
    "off":    1.0,
    "light":  0.90,
    "medium": 0.82,
}

# ── Brightness (shadows-only) ──────────────────────────────────────────
# Uses ffmpeg's "curves" filter to build a shadow-recovery curve: it only
# lifts the darkest pixel values and tapers back to no change by midtones,
# leaving well-lit/bright content essentially untouched.
#
# Points are normalized to [0,1] (the curves filter requires that, not
# raw 0-255 values).
#   minor:  lifts near-black (0) by ~5/255  (~2%), tapering to 0 by mid-gray
#   medium: lifts near-black (0) by ~13/255 (~5%), tapering to 0 by mid-gray
BRIGHTEN_FILTERS = {
    "minor":  "curves=master='0/0.0196 0.502/0.502 1/1'",
    "medium": "curves=master='0/0.051 0.502/0.502 1/1'",
}

# IMPORTANT: "curves" (and some other filters) internally upconvert to a
# higher-precision planar format like YUV444P. av1_nvenc rejects that
# ("YUV444P not supported" / "No capable devices found") since NVENC only
# accepts 4:2:0-family formats. Forcing format=yuv420p at the end of the
# filter chain whenever any video filter is active avoids this.
POST_FILTER_FORMAT = "format=yuv420p"

# How long (seconds) to wait after sending a graceful "q" quit command
# before giving up and force-terminating the ffmpeg process.
GRACEFUL_STOP_TIMEOUT = 12

# ── Size-estimate calibration ────────────────────────────────────────────
# Calibration point from real-world feedback: estimated ~900 MB, actual
# output was ~2.15 GB (ratio ≈ 2.45x).
BASE_BPP_AT_CQ32 = 0.045
CALIBRATION_FACTOR = 2.45

PRESET_SIZE_FACTOR = {
    "p1": 1.15, "p2": 1.10, "p3": 1.07, "p4": 1.03,
    "p5": 1.00, "p6": 0.97, "p7": 0.93,
}

AUDIO_BITRATES = {
    "aac 128k": 128_000,
    "aac 256k": 256_000,
    "aac 384k": 384_000,
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def parse_google_photos_date(filename: str):
    """
    Extract date from Google Photos filenames like:
      PXL_20260430_094807919.mp4
      VID_20251225_120000.mp4
    Returns a datetime or None.
    """
    stem = Path(filename).stem
    m = re.search(r"(\d{8})_(\d{6})", stem)
    if m:
        try:
            dt_str = m.group(1) + m.group(2)
            return datetime.datetime.strptime(dt_str, "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return None


def format_size(bytes_val):
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def format_duration(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def probe_media_info(path: str):
    """
    Run ffprobe on a file and return a dict of useful details.
    Returns None if ffprobe isn't available or fails.
    """
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=15)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except Exception:
        return None

    info = {
        "duration": None, "width": None, "height": None,
        "fps": None, "vcodec": None, "acodec": None,
        "abitrate": None, "vbitrate": None, "overall_bitrate": None,
    }

    fmt = data.get("format", {})
    if fmt.get("duration"):
        try:
            info["duration"] = float(fmt["duration"])
        except ValueError:
            pass
    if fmt.get("bit_rate"):
        try:
            info["overall_bitrate"] = int(fmt["bit_rate"])
        except ValueError:
            pass

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and info["vcodec"] is None:
            info["vcodec"] = stream.get("codec_name")
            info["width"] = stream.get("width")
            info["height"] = stream.get("height")
            rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
            if rate and rate != "0/0":
                try:
                    num, den = rate.split("/")
                    den = float(den)
                    info["fps"] = round(float(num) / den, 2) if den else None
                except Exception:
                    pass
            if stream.get("bit_rate"):
                try:
                    info["vbitrate"] = int(stream["bit_rate"])
                except ValueError:
                    pass
        elif stream.get("codec_type") == "audio" and info["acodec"] is None:
            info["acodec"] = stream.get("codec_name")
            if stream.get("bit_rate"):
                try:
                    info["abitrate"] = int(stream["bit_rate"])
                except ValueError:
                    pass

    return info


def estimate_output_bytes(info: dict, cq: int, preset: str, audio: str, denoise_mode: str):
    """
    Heuristic size estimate for a CQ-mode AV1/NVENC encode. Treat as
    ballpark only.
    """
    if not info:
        return None
    duration = info.get("duration")
    w, h = info.get("width"), info.get("height")
    if not duration or not w or not h:
        return None

    fps = info.get("fps") or 30.0
    pixels_per_frame = w * h

    bpp = BASE_BPP_AT_CQ32 * (2 ** ((32 - cq) / 6))
    bpp *= PRESET_SIZE_FACTOR.get(preset, 1.0)
    bpp *= DENOISE_BPP_FACTOR.get(denoise_mode, 1.0)

    video_bitrate = bpp * pixels_per_frame * fps       # bits/sec
    video_bytes = (video_bitrate * duration / 8) * CALIBRATION_FACTOR

    if audio == "copy":
        abitrate = info.get("abitrate") or 128_000
    else:
        abitrate = AUDIO_BITRATES.get(audio, 128_000)
    audio_bytes = abitrate * duration / 8

    return video_bytes + audio_bytes


# ─── Main App ────────────────────────────────────────────────────────────────

class AV1Compressor(tk.Tk):

    def __init__(self):
        super().__init__()

        self.title("AV1 Video Compressor — NVENC")
        self.geometry("1040x820")
        self.minsize(820, 660)
        self.configure(bg=BG)

        # State
        self.queue_items  = []      # list of file paths
        self.file_info    = {}      # path -> probe_media_info() dict
        self.log_queue    = queue.Queue()
        self.is_running   = False
        self.current_proc = None
        self.stop_flag    = threading.Event()

        # Live-line tracking for in-place ffmpeg progress updates.
        self._live_active = False

        self._setup_styles()
        self._build_ui()
        self._try_enable_dnd()
        self._poll_log()

    # ── Style ─────────────────────────────────────────────────────────────

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".",
            background=BG, foreground=TEXT,
            fieldbackground=SURFACE, bordercolor=BORDER,
            troughcolor=SURFACE2, selectbackground=ACCENT_DIM,
            selectforeground=TEXT, relief="flat",
            font=FONT_BODY)

        style.configure("TFrame",  background=BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Surface2.TFrame", background=SURFACE2)

        style.configure("TLabel",  background=BG,      foreground=TEXT, font=FONT_BODY)
        style.configure("Muted.TLabel", background=BG, foreground=TEXT_MUTED, font=FONT_SMALL)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=FONT_TITLE)
        style.configure("Accent.TLabel", background=BG, foreground=ACCENT, font=FONT_TITLE)
        style.configure("Estimate.TLabel", background=BG, foreground=ACCENT, font=FONT_TITLE)

        style.configure("TButton",
            background=SURFACE2, foreground=TEXT,
            bordercolor=BORDER, focuscolor=ACCENT,
            padding=(12, 6), relief="flat")
        style.map("TButton",
            background=[("active", BORDER), ("pressed", BORDER)],
            relief=[("pressed", "flat")])

        style.configure("Primary.TButton",
            background=ACCENT, foreground="#0d1a1b",
            bordercolor=ACCENT, padding=(14, 8))
        style.map("Primary.TButton",
            background=[("active", "#227f8b"), ("pressed", "#1a626b"), ("disabled", SURFACE2)],
            foreground=[("disabled", TEXT_FAINT)])

        style.configure("Danger.TButton",
            background="#a53142", foreground=TEXT,
            bordercolor="#a53142", padding=(12, 6))
        style.map("Danger.TButton",
            background=[("active", "#782b33"), ("pressed", "#521f24")])

        style.configure("Treeview",
            background=SURFACE, foreground=TEXT,
            fieldbackground=SURFACE, bordercolor=BORDER,
            rowheight=28, font=FONT_BODY)
        style.configure("Treeview.Heading",
            background=SURFACE2, foreground=TEXT_MUTED,
            bordercolor=BORDER, relief="flat", font=FONT_SMALL)
        style.map("Treeview",
            background=[("selected", ACCENT_DIM)],
            foreground=[("selected", TEXT)])

        style.configure("TProgressbar",
            background=ACCENT, troughcolor=SURFACE2,
            bordercolor=BORDER, thickness=4)

        style.configure("TEntry",
            fieldbackground=SURFACE2, foreground=TEXT,
            bordercolor=BORDER, padding=(8, 4))
        style.map("TEntry", bordercolor=[("focus", ACCENT)])

        style.configure("TCombobox",
            fieldbackground=SURFACE2, foreground=TEXT,
            background=SURFACE2, selectbackground=ACCENT_DIM,
            selectforeground=TEXT, bordercolor=BORDER,
            arrowcolor=TEXT, insertcolor=TEXT)
        style.map("TCombobox",
            fieldbackground=[("readonly", SURFACE2), ("disabled", SURFACE)],
            foreground=[("readonly", TEXT), ("disabled", TEXT_MUTED)],
            background=[("readonly", SURFACE2), ("active", SURFACE2)],
            selectbackground=[("readonly", SURFACE2)],
            selectforeground=[("readonly", TEXT)])
        self.option_add("*TCombobox*Listbox.background", SURFACE2)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT_DIM)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.option_add("*TCombobox*Listbox.font", FONT_BODY)

        style.configure("TSpinbox",
            fieldbackground=SURFACE2, foreground=TEXT,
            bordercolor=BORDER, background=SURFACE2)

        style.configure("TCheckbutton",
            background=BG, foreground=TEXT, font=FONT_BODY)
        style.map("TCheckbutton",
            background=[("active", BG)],
            foreground=[("disabled", TEXT_FAINT)])

        style.configure("TLabelframe",
            background=BG, foreground=TEXT_MUTED,
            bordercolor=BORDER, relief="flat")
        style.configure("TLabelframe.Label",
            background=BG, foreground=TEXT_MUTED, font=FONT_SMALL)

        style.configure("TScrollbar",
            background=SURFACE2, troughcolor=SURFACE,
            bordercolor=BORDER, arrowcolor=TEXT_MUTED)

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header
        header = ttk.Frame(self, style="TFrame")
        header.pack(fill="x", padx=16, pady=(14, 0))

        ttk.Label(header, text="⬡", style="Accent.TLabel",
                  font=("Segoe UI", 20)).pack(side="left", padx=(0, 10))

        title_col = ttk.Frame(header, style="TFrame")
        title_col.pack(side="left")
        ttk.Label(title_col, text="AV1 Video Compressor",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_col,
                  text="NVENC hardware encoding  •  Metadata-preserving  •  Google Photos ready",
                  style="Muted.TLabel").pack(anchor="w")

        # ── Settings row
        cfg = ttk.LabelFrame(self, text="Encoding Settings", padding=(12, 8))
        cfg.pack(fill="x", padx=16, pady=(12, 0))

        # CQ
        ttk.Label(cfg, text="CQ (quality):").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.cq_var = tk.IntVar(value=32)
        cq_spin = ttk.Spinbox(cfg, from_=0, to=51, textvariable=self.cq_var,
                               width=5, font=FONT_BODY,
                               command=self._on_settings_changed)
        cq_spin.grid(row=0, column=1, sticky="w", padx=(0, 16))
        ttk.Label(cfg, text="(lower = better, 0–51)",
                  style="Muted.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 24))

        # Preset
        ttk.Label(cfg, text="Preset:").grid(row=0, column=3, sticky="w", padx=(0, 6))
        self.preset_var = tk.StringVar(value="p7")
        preset_cb = ttk.Combobox(cfg, textvariable=self.preset_var,
                                  values=["p1","p2","p3","p4","p5","p6","p7"],
                                  width=5, state="readonly", font=FONT_BODY)
        preset_cb.set(self.preset_var.get())
        preset_cb.grid(row=0, column=4, sticky="w", padx=(0, 4))
        ttk.Label(cfg, text="(p1=fast  p7=best)",
                  style="Muted.TLabel").grid(row=0, column=5, sticky="w", padx=(0, 24))

        # Audio
        ttk.Label(cfg, text="Audio:").grid(row=0, column=6, sticky="w", padx=(0, 6))
        self.audio_var = tk.StringVar(value="copy")
        audio_cb = ttk.Combobox(cfg, textvariable=self.audio_var,
                                 values=["copy", "aac 128k", "aac 256k", "aac 384k"],
                                 width=10, state="readonly", font=FONT_BODY)
        audio_cb.set(self.audio_var.get())
        audio_cb.grid(row=0, column=7, sticky="w", padx=(0, 16))

        # Output suffix
        ttk.Label(cfg, text="Output suffix:").grid(row=0, column=8, sticky="w", padx=(0, 6))
        self.suffix_var = tk.StringVar(value="_av1")
        ttk.Entry(cfg, textvariable=self.suffix_var, width=8, font=FONT_BODY,
                  style="TEntry").grid(row=0, column=9, sticky="w")

        # Denoise toggles — Light / Medium, CPU-based (hqdn3d).
        self.denoise_light_var  = tk.BooleanVar(value=False)
        self.denoise_medium_var = tk.BooleanVar(value=False)

        denoise_light_chk = ttk.Checkbutton(
            cfg, text="Light denoise",
            variable=self.denoise_light_var,
            style="TCheckbutton",
            command=lambda: self._on_denoise_toggle("light"))
        denoise_light_chk.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        denoise_medium_chk = ttk.Checkbutton(
            cfg, text="Medium denoise",
            variable=self.denoise_medium_var,
            style="TCheckbutton",
            command=lambda: self._on_denoise_toggle("medium"))
        denoise_medium_chk.grid(row=1, column=2, columnspan=2, sticky="w", pady=(8, 0))

        # Brighten toggles — Minor (+2%) / Medium (+5%), shadows-only.
        self.brighten_minor_var  = tk.BooleanVar(value=False)
        self.brighten_medium_var = tk.BooleanVar(value=False)

        brighten_minor_chk = ttk.Checkbutton(
            cfg, text="Brighten shadows +2%",
            variable=self.brighten_minor_var,
            style="TCheckbutton",
            command=lambda: self._on_brighten_toggle("minor"))
        brighten_minor_chk.grid(row=1, column=4, columnspan=2, sticky="w", pady=(8, 0))

        brighten_medium_chk = ttk.Checkbutton(
            cfg, text="Brighten shadows +5%",
            variable=self.brighten_medium_var,
            style="TCheckbutton",
            command=lambda: self._on_brighten_toggle("medium"))
        brighten_medium_chk.grid(row=1, column=6, columnspan=2, sticky="w", pady=(8, 0))

        # Estimated total output size (updates live as settings/queue change)
        self.estimate_lbl = ttk.Label(cfg, text="Est. total output: —",
                                       style="Estimate.TLabel")
        self.estimate_lbl.grid(row=2, column=0, columnspan=10, sticky="e", pady=(10, 0))
        ttk.Label(cfg, text="Brighten only lifts dark/shadow pixels — bright scenes are left unchanged.",
                  style="Muted.TLabel").grid(row=3, column=0, columnspan=6, sticky="w")
        ttk.Label(cfg, text="(rough estimate — see note below log)",
                  style="Muted.TLabel").grid(row=3, column=6, columnspan=4, sticky="e")
        cfg.grid_columnconfigure(9, weight=1)

        # React to CQ / preset / audio changes to refresh estimates.
        self.cq_var.trace_add("write", lambda *a: self._on_settings_changed())
        self.preset_var.trace_add("write", lambda *a: self._on_settings_changed())
        self.audio_var.trace_add("write", lambda *a: self._on_settings_changed())

        # ── Drop zone
        self.drop_frame = tk.Frame(self, bg=SURFACE, highlightbackground=BORDER,
                                   highlightthickness=1, cursor="hand2")
        self.drop_frame.pack(fill="x", padx=16, pady=(12, 0))
        self.drop_frame.bind("<Button-1>", lambda e: self._browse_files())

        self.drop_label = tk.Label(
            self.drop_frame,
            text="⬇  Drag & drop video files here  —  or click to browse",
            bg=SURFACE, fg=TEXT_MUTED, font=("Segoe UI", 10),
            pady=18)
        self.drop_label.pack(fill="x")
        self.drop_label.bind("<Button-1>", lambda e: self._browse_files())

        # ── Queue table
        queue_header = ttk.Frame(self, style="TFrame")
        queue_header.pack(fill="x", padx=16, pady=(12, 4))
        ttk.Label(queue_header, text="Queue", style="Title.TLabel").pack(side="left")
        self.queue_count_lbl = ttk.Label(queue_header, text="0 files",
                                          style="Muted.TLabel")
        self.queue_count_lbl.pack(side="left", padx=8)
        ttk.Button(queue_header, text="Clear All",
                   command=self._clear_queue).pack(side="right")
        ttk.Button(queue_header, text="Remove Selected",
                   command=self._remove_selected).pack(side="right", padx=(0, 6))

        tree_frame = ttk.Frame(self, style="Surface.TFrame")
        tree_frame.pack(fill="x", padx=16)

        cols = ("file", "size", "est_size", "date", "status")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                  height=7, selectmode="extended")
        self.tree.heading("file",     text="Filename")
        self.tree.heading("size",     text="Size")
        self.tree.heading("est_size", text="Est. Output")
        self.tree.heading("date",     text="Detected Date")
        self.tree.heading("status",   text="Status")
        self.tree.column("file",     width=340, stretch=True)
        self.tree.column("size",     width=80,  stretch=False, anchor="e")
        self.tree.column("est_size", width=100, stretch=False, anchor="e")
        self.tree.column("date",     width=150, stretch=False)
        self.tree.column("status",   width=150, stretch=False)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("pending",  foreground=TEXT_MUTED)
        self.tree.tag_configure("active",   foreground=ACCENT)
        self.tree.tag_configure("done",     foreground=SUCCESS)
        self.tree.tag_configure("error",    foreground=ERROR)
        self.tree.tag_configure("skipped",  foreground=WARNING)

        # ── Progress
        progress_row = ttk.Frame(self, style="TFrame")
        progress_row.pack(fill="x", padx=16, pady=(10, 0))

        self.progress_lbl = ttk.Label(progress_row, text="Ready",
                                       style="Muted.TLabel")
        self.progress_lbl.pack(side="left")

        self.time_lbl = ttk.Label(progress_row, text="",
                                   style="Muted.TLabel")
        self.time_lbl.pack(side="right")

        self.progress_bar = ttk.Progressbar(self, mode="determinate",
                                             style="TProgressbar")
        self.progress_bar.pack(fill="x", padx=16, pady=(4, 0))

        # ── Action buttons
        btn_row = ttk.Frame(self, style="TFrame")
        btn_row.pack(fill="x", padx=16, pady=(10, 0))

        self.start_btn = ttk.Button(btn_row, text="▶  Start Encoding",
                                    style="Primary.TButton",
                                    command=self._start_encoding)
        self.start_btn.pack(side="left")

        self.stop_btn = ttk.Button(btn_row, text="■  Stop",
                                   style="Danger.TButton",
                                   command=self._stop_encoding,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        self.open_out_btn = ttk.Button(btn_row, text="📂  Open Output Folder",
                                       command=self._open_output_folder,
                                       state="disabled")
        self.open_out_btn.pack(side="right")

        # ── Log
        log_header = ttk.Frame(self, style="TFrame")
        log_header.pack(fill="x", padx=16, pady=(12, 4))
        ttk.Label(log_header, text="Log", style="Title.TLabel").pack(side="left")
        ttk.Button(log_header, text="Clear Log",
                   command=self._clear_log).pack(side="right")
        ttk.Button(log_header, text="Copy Log",
                   command=self._copy_log).pack(side="right", padx=(0, 6))

        log_frame = tk.Frame(self, bg=SURFACE, highlightbackground=BORDER,
                              highlightthickness=1)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        self.log_text = tk.Text(
            log_frame,
            bg=SURFACE, fg=TEXT, font=FONT_MONO,
            relief="flat", state="disabled",
            wrap="none", padx=10, pady=8,
            selectbackground=ACCENT_DIM, selectforeground=TEXT,
            insertbackground=TEXT)
        log_vsb = ttk.Scrollbar(log_frame, orient="vertical",
                                  command=self.log_text.yview)
        log_hsb = ttk.Scrollbar(log_frame, orient="horizontal",
                                  command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=log_vsb.set,
                                 xscrollcommand=log_hsb.set)
        log_hsb.pack(side="bottom", fill="x")
        log_vsb.pack(side="right",  fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        # Configure log tags
        self.log_text.tag_configure("ts",      foreground=TEXT_FAINT)
        self.log_text.tag_configure("info",    foreground=TEXT)
        self.log_text.tag_configure("success", foreground=SUCCESS)
        self.log_text.tag_configure("warn",    foreground=WARNING)
        self.log_text.tag_configure("error",   foreground=ERROR)
        self.log_text.tag_configure("accent",  foreground=ACCENT)
        self.log_text.tag_configure("muted",   foreground=TEXT_MUTED)
        self.log_text.tag_configure("ffmpeg",  foreground=TEXT_FAINT,
                                               font=("Consolas", 10))

        self._log("AV1 Video Compressor ready.", "success")
        self._log("Drag & drop videos into the queue, configure settings, then press Start.", "muted")
        self._log("Size estimates are heuristic (constqp ≠ fixed bitrate) — treat as ballpark.", "muted")
        self._log("Denoise (light/medium) runs on CPU via hqdn3d.", "muted")
        self._log("Brighten only lifts shadow/dark pixels via a curves-based shadow recovery — bright scenes are unaffected.", "muted")
        self._log("Stop sends ffmpeg a graceful quit so the partial output stays playable.", "muted")
        self._log("─" * 80, "muted")

    # ── Denoise toggle logic ────────────────────────────────────────────

    def _on_denoise_toggle(self, which):
        """Enforce mutual exclusivity between the Light and Medium denoise checkboxes."""
        if which == "light" and self.denoise_light_var.get():
            self.denoise_medium_var.set(False)
        elif which == "medium" and self.denoise_medium_var.get():
            self.denoise_light_var.set(False)
        self._on_settings_changed()

    def _current_denoise_mode(self):
        if self.denoise_light_var.get():
            return "light"
        if self.denoise_medium_var.get():
            return "medium"
        return "off"

    # ── Brighten toggle logic ────────────────────────────────────────────

    def _on_brighten_toggle(self, which):
        """Enforce mutual exclusivity between the +2% and +5% brighten checkboxes."""
        if which == "minor" and self.brighten_minor_var.get():
            self.brighten_medium_var.set(False)
        elif which == "medium" and self.brighten_medium_var.get():
            self.brighten_minor_var.set(False)
        self._on_settings_changed()

    def _current_brighten_mode(self):
        if self.brighten_minor_var.get():
            return "minor"
        if self.brighten_medium_var.get():
            return "medium"
        return "off"

    # ── DnD ───────────────────────────────────────────────────────────────

    def _try_enable_dnd(self):
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            self.drop_frame.drop_target_register(DND_FILES)
            self.drop_frame.dnd_bind("<<Drop>>", self._on_dnd_drop)
            self.tree.drop_target_register(DND_FILES)
            self.tree.dnd_bind("<<Drop>>", self._on_dnd_drop)
            self._log("✓ Drag & drop enabled (tkinterdnd2 detected).", "success")
        except Exception:
            self._log("ℹ  tkinterdnd2 not installed — drag & drop unavailable.", "warn")
            self._log("   Install with: pip install tkinterdnd2", "muted")
            self._log("   Using click-to-browse mode instead.", "muted")

    def _on_dnd_drop(self, event):
        raw = event.data
        paths = []
        for token in re.findall(r'\{[^}]+\}|\S+', raw):
            paths.append(token.strip("{}"))
        video_exts = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".wmv", ".webm",
                      ".mts", ".m2ts", ".ts"}
        added = 0
        for p in paths:
            if Path(p).suffix.lower() in video_exts:
                if p not in self.queue_items:
                    self._add_to_queue(p)
                    added += 1
        if added:
            self._log(f"+ Added {added} file(s) via drag & drop.", "accent")

    # ── Queue ─────────────────────────────────────────────────────────────

    def _browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Select video files",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.avi *.m4v *.wmv *.webm *.mts *.m2ts *.ts"),
                ("All files", "*.*")])
        added = 0
        for p in paths:
            if p not in self.queue_items:
                self._add_to_queue(p)
                added += 1
        if added:
            self._log(f"+ Added {added} file(s) via browse.", "accent")

    def _add_to_queue(self, path):
        p = Path(path)
        size_bytes = p.stat().st_size if p.exists() else 0
        size_str = format_size(size_bytes) if p.exists() else "?"
        dt = parse_google_photos_date(p.name)
        date_str = dt.strftime("%Y-%m-%d  %H:%M:%S") if dt else "⚠ Not detected"
        tag = "pending" if dt else "skipped"

        iid = self.tree.insert("", "end",
            values=(p.name, size_str, "—", date_str, "Pending"),
            tags=(tag,))
        self.queue_items.append(path)
        self._update_queue_label()

        # Log full details of the input file as it's added.
        self._log(f"+ Queued: {p.name}", "accent")
        self._log(f"    Path: {p}", "muted")
        self._log(f"    Size: {size_str}", "muted")
        if dt:
            self._log(f"    Detected date: {dt.strftime('%Y-%m-%d %H:%M:%S')}", "muted")
        else:
            self._log("    Detected date: none (no PXL_/VID_ pattern found)", "warn")

        info = probe_media_info(str(p))
        self.file_info[path] = info
        if info:
            if info["duration"] is not None:
                self._log(f"    Duration: {format_duration(info['duration'])}", "muted")
            if info["width"] and info["height"]:
                res_line = f"    Resolution: {info['width']}x{info['height']}"
                if info["fps"]:
                    res_line += f" @ {info['fps']} fps"
                self._log(res_line, "muted")
            if info["vcodec"]:
                vline = f"    Video codec: {info['vcodec']}"
                if info["vbitrate"]:
                    vline += f" ({format_size(info['vbitrate'] / 8)}/s)"
                self._log(vline, "muted")
            if info["acodec"]:
                aline = f"    Audio codec: {info['acodec']}"
                if info["abitrate"]:
                    aline += f" ({format_size(info['abitrate'] / 8)}/s)"
                self._log(aline, "muted")
            if info["overall_bitrate"]:
                self._log(f"    Overall bitrate: {format_size(info['overall_bitrate'] / 8)}/s",
                           "muted")
        else:
            self._log("    (ffprobe unavailable — media details not read)", "warn")

        self._refresh_estimates()
        return iid

    def _remove_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        for iid in selected:
            idx = self.tree.index(iid)
            if idx < len(self.queue_items):
                removed_path = self.queue_items.pop(idx)
                self.file_info.pop(removed_path, None)
            self.tree.delete(iid)
        self._update_queue_label()
        self._refresh_estimates()

    def _clear_queue(self):
        if self.is_running:
            messagebox.showwarning("Running", "Stop encoding before clearing the queue.")
            return
        self.tree.delete(*self.tree.get_children())
        self.queue_items.clear()
        self.file_info.clear()
        self._update_queue_label()
        self.progress_bar["value"] = 0
        self.progress_lbl.config(text="Ready")
        self._refresh_estimates()

    def _update_queue_label(self):
        n = len(self.queue_items)
        self.queue_count_lbl.config(text=f"{n} file{'s' if n != 1 else ''}")

    def _set_item_status(self, iid, status, tag):
        vals = list(self.tree.item(iid, "values"))
        vals[4] = status
        self.tree.item(iid, values=vals, tags=(tag,))
        self.tree.see(iid)

    # ── Live size estimation ────────────────────────────────────────────

    def _on_settings_changed(self):
        self._refresh_estimates()

    def _refresh_estimates(self):
        """Recompute the per-row and total estimated output size."""
        try:
            cq = self.cq_var.get()
        except (tk.TclError, ValueError):
            return
        preset = self.preset_var.get()
        audio = self.audio_var.get()
        denoise_mode = self._current_denoise_mode()

        total_bytes = 0.0
        unknown = 0
        items = self.tree.get_children()

        for idx, iid in enumerate(items):
            if idx >= len(self.queue_items):
                continue
            path = self.queue_items[idx]
            info = self.file_info.get(path)
            est = estimate_output_bytes(info, cq, preset, audio, denoise_mode)
            vals = list(self.tree.item(iid, "values"))
            if est is not None:
                vals[2] = f"~{format_size(est)}"
                total_bytes += est
            else:
                vals[2] = "n/a"
                unknown += 1
            self.tree.item(iid, values=vals)

        n = len(self.queue_items)
        if n == 0:
            self.estimate_lbl.config(text="Est. total output: —")
        elif unknown == n:
            self.estimate_lbl.config(text="Est. total output: n/a (no ffprobe data)")
        else:
            note = f"  ({unknown} file(s) unestimated)" if unknown else ""
            self.estimate_lbl.config(
                text=f"Est. total output: ~{format_size(total_bytes)}{note}")

    # ── Encoding ──────────────────────────────────────────────────────────

    def _build_ffmpeg_cmd(self, input_path: str, output_path: str,
                           creation_time_str: str = None) -> list:
        cq            = self._encode_params["cq"]
        preset        = self._encode_params["preset"]
        audio         = self._encode_params["audio"]
        denoise_mode  = self._encode_params["denoise_mode"]
        brighten_mode = self._encode_params["brighten_mode"]

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
        ]

        # Metadata injection
        cmd += ["-map_metadata", "0", "-movflags", "use_metadata_tags"]

        if creation_time_str:
            cmd += ["-metadata", f"creation_time={creation_time_str}"]

        # Combine any active video filters (denoise, brighten) into a
        # single -vf chain — ffmpeg only honors the last -vf if given
        # multiple times, so they must be comma-joined into one filter.
        # A trailing format=yuv420p is added whenever any filter runs,
        # since filters like "curves" can upconvert internally to
        # YUV444P, which av1_nvenc rejects outright.
        vf_parts = []
        if denoise_mode in DENOISE_FILTERS:
            vf_parts.append(DENOISE_FILTERS[denoise_mode])
        if brighten_mode in BRIGHTEN_FILTERS:
            vf_parts.append(BRIGHTEN_FILTERS[brighten_mode])
        if vf_parts:
            vf_parts.append(POST_FILTER_FORMAT)
            cmd += ["-vf", ",".join(vf_parts)]

        # Video
        cmd += [
            "-c:v", "av1_nvenc",
            "-cq", str(cq),
            "-preset", preset,
            "-tune", "hq",
        ]

        # Audio
        if audio == "copy":
            cmd += ["-c:a", "copy"]
        elif audio == "aac 128k":
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        elif audio == "aac 256k":
            cmd += ["-c:a", "aac", "-b:a", "256k"]
        elif audio == "aac 384k":
            cmd += ["-c:a", "aac", "-b:a", "384k"]

        cmd += [output_path]
        return cmd

    def _start_encoding(self):
        if not self.queue_items:
            messagebox.showinfo("Empty Queue", "Add some video files to the queue first.")
            return

        self._encode_params = {
            "cq":            self.cq_var.get(),
            "preset":        self.preset_var.get(),
            "audio":         self.audio_var.get(),
            "suffix":        self.suffix_var.get(),
            "denoise_mode":  self._current_denoise_mode(),
            "brighten_mode": self._current_brighten_mode(),
        }

        self.is_running   = True
        self.stop_flag.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.open_out_btn.config(state="disabled")
        self._start_time = datetime.datetime.now()

        thread = threading.Thread(target=self._encode_all, daemon=True)
        thread.start()

    def _stop_encoding(self):
        """
        Request a graceful stop. Instead of killing ffmpeg outright (which
        skips writing the MP4 trailer/moov atom and leaves the file
        unplayable), send the same 'q' keypress ffmpeg listens for
        interactively. This tells it to stop reading new frames, flush the
        encoder, and finalize the file normally — so the partial output is
        playable up to the point it was stopped.
        """
        self.stop_flag.set()
        proc = self.current_proc
        if proc and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write("q")
                    proc.stdin.flush()
                self._log("■  Stop requested — sending graceful quit (q) so the "
                          "partial file is finalized and playable…", "warn")
            except Exception:
                self._log("■  Could not send graceful quit — forcing termination "
                          "(output may not be playable).", "warn")
                proc.terminate()
                self.stop_btn.config(state="disabled")
                return

            # Safety net: if ffmpeg doesn't exit within the grace period
            # (hung filter, unresponsive process, etc.), force-terminate.
            watchdog = threading.Thread(
                target=self._force_stop_after_timeout, args=(proc,), daemon=True)
            watchdog.start()

        self.stop_btn.config(state="disabled")

    def _force_stop_after_timeout(self, proc, timeout=GRACEFUL_STOP_TIMEOUT):
        waited = 0.0
        interval = 0.5
        while waited < timeout:
            if proc.poll() is not None:
                return  # already exited cleanly
            time.sleep(interval)
            waited += interval
        if proc.poll() is None:
            self.log_queue.put(("log", (
                f"  ⚠  ffmpeg didn't respond to graceful quit within {timeout}s — "
                "forcing termination (file may be truncated).", "warn")))
            proc.terminate()

    def _encode_all(self):
        items = list(self.tree.get_children())
        total = len(items)
        done  = 0

        self.log_queue.put(("separator", None))
        dm = self._encode_params["denoise_mode"]
        bm = self._encode_params["brighten_mode"]
        denoise_tag = f"  denoise={dm}" if dm != "off" else ""
        brighten_tag = f"  brighten={bm}" if bm != "off" else ""
        self.log_queue.put(("log", (f"▶  Starting batch encode — {total} file(s)  |  "
                                    f"CQ={self._encode_params['cq']}  preset={self._encode_params['preset']}  "
                                    f"audio={self._encode_params['audio']}{denoise_tag}{brighten_tag}", "accent")))

        last_output_dir = None

        for idx, iid in enumerate(items):
            if self.stop_flag.is_set():
                self.log_queue.put(("log", ("■  Stopped by user.", "warn")))
                break

            path = self.queue_items[idx]
            p    = Path(path)
            suffix = self._encode_params.get("suffix") or "_av1"
            out_path = p.with_name(p.stem + suffix + ".mp4")
            last_output_dir = str(p.parent)

            dt = parse_google_photos_date(p.name)
            ct_str = dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z") if dt else None

            self.log_queue.put(("log", (f"\n[{idx+1}/{total}]  {p.name}", "accent")))
            if ct_str:
                self.log_queue.put(("log", (f"  📅 Date from filename: {dt.strftime('%Y-%m-%d %H:%M:%S')}", "muted")))
            else:
                self.log_queue.put(("log", ("  ⚠  No date in filename — falling back to embedded metadata only", "warn")))
            self.log_queue.put(("log", (f"  📂 Output: {out_path.name}", "muted")))
            self.log_queue.put(("set_status", (iid, "Encoding…", "active")))
            self.log_queue.put(("progress", (idx, total)))

            cmd = self._build_ffmpeg_cmd(str(p), str(out_path), ct_str)
            self.log_queue.put(("log", (f"  $ {' '.join(cmd)}", "ffmpeg")))

            t_start = datetime.datetime.now()

            try:
                self.current_proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace"
                )

                for line in self.current_proc.stdout:
                    line = line.rstrip()
                    if not line:
                        continue
                    if line.startswith("frame="):
                        self.log_queue.put(("live", line))
                    elif any(kw in line for kw in (
                            "Error", "error", "Invalid", "No such",
                            "Cannot", "failed", "Unknown")):
                        self.log_queue.put(("log", (f"  {line}", "error")))
                    else:
                        self.log_queue.put(("log", (f"  {line}", "ffmpeg")))

                self.current_proc.wait()
                retcode = self.current_proc.returncode

            except FileNotFoundError:
                self.log_queue.put(("log", ("  ✗ ffmpeg not found — is it installed and on PATH?", "error")))
                self.log_queue.put(("set_status", (iid, "Error", "error")))
                continue
            except Exception as e:
                self.log_queue.put(("log", (f"  ✗ Exception: {e}", "error")))
                self.log_queue.put(("set_status", (iid, "Error", "error")))
                continue

            elapsed = (datetime.datetime.now() - t_start).total_seconds()

            if retcode == 0 and out_path.exists():
                in_sz  = p.stat().st_size
                out_sz = out_path.stat().st_size
                saving = (1 - out_sz / in_sz) * 100 if in_sz else 0
                self.log_queue.put(("log", (
                    f"  ✓ Done in {format_duration(elapsed)}  |  "
                    f"{format_size(in_sz)} → {format_size(out_sz)}  "
                    f"({saving:.1f}% smaller)", "success")))
                self.log_queue.put(("set_status",
                    (iid, f"✓ Done  {saving:.0f}% saved", "done")))
                done += 1
            elif self.stop_flag.is_set():
                if out_path.exists():
                    out_sz = out_path.stat().st_size
                    self.log_queue.put(("log", (
                        f"  ⏹ Stopped — partial file finalized and should be playable "
                        f"({format_size(out_sz)}).", "warn")))
                else:
                    self.log_queue.put(("log", (
                        "  ⏹ Stopped — no output file was written (stopped too early).", "warn")))
                self.log_queue.put(("set_status", (iid, "Stopped", "skipped")))
            else:
                self.log_queue.put(("log", (f"  ✗ ffmpeg exited with code {retcode}", "error")))
                self.log_queue.put(("set_status", (iid, "Error", "error")))

        total_elapsed = (datetime.datetime.now() - self._start_time).total_seconds()
        self.log_queue.put(("separator", None))
        self.log_queue.put(("log", (
            f"✓  Batch complete — {done}/{total} files encoded successfully  |  "
            f"Total time: {format_duration(total_elapsed)}", "success")))
        self.log_queue.put(("done", last_output_dir))

    # ── Log polling (runs on main thread) ────────────────────────────────

    def _poll_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                kind, data = msg

                if kind == "log":
                    text, tag = data
                    self._log(text, tag)

                elif kind == "live":
                    self._update_live(data)

                elif kind == "set_status":
                    iid, status, tag = data
                    self._set_item_status(iid, status, tag)

                elif kind == "progress":
                    idx, total = data
                    pct = int((idx / total) * 100)
                    self.progress_bar["value"] = pct
                    self.progress_lbl.config(text=f"Encoding {idx+1} of {total}…")

                elif kind == "separator":
                    self._log("─" * 80, "muted")

                elif kind == "done":
                    output_dir = data
                    self.is_running = False
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.progress_bar["value"] = 100
                    self.progress_lbl.config(text="Complete")
                    if output_dir:
                        self._last_output_dir = output_dir
                        self.open_out_btn.config(state="normal")

        except queue.Empty:
            pass

        self.after(50, self._poll_log)

    def _log(self, message: str, tag: str = "info"):
        """Append a normal (non-live) log line. Ends any active live-update block."""
        self._live_active = False
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{ts}]  ", "ts")
        self.log_text.insert("end", message + "\n", tag)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _update_live(self, line: str):
        """
        Overwrite the current ffmpeg progress line in place.

        Uses an explicit mark ('live_start') rather than guessing offsets like
        'end-2l', so interleaved non-live log lines (which reset
        self._live_active in _log) can never cause this to delete or corrupt
        unrelated log content.
        """
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state="normal")

        if self._live_active:
            self.log_text.delete("live_start", "end")
        else:
            if self.log_text.index("end-1c") != "1.0":
                last_char = self.log_text.get("end-2c", "end-1c")
                if last_char != "\n":
                    self.log_text.insert("end", "\n")
            self.log_text.mark_set("live_start", "end-1c")
            self.log_text.mark_gravity("live_start", "left")
            self._live_active = True

        self.log_text.insert("end", f"[{ts}]  ", "ts")
        self.log_text.insert("end", f"  {line}\n", "ffmpeg")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self._live_active = False

    def _copy_log(self):
        content = self.log_text.get("1.0", "end")
        self.clipboard_clear()
        self.clipboard_append(content)

    # ── Utils ─────────────────────────────────────────────────────────────

    def _open_output_folder(self):
        folder = getattr(self, "_last_output_dir", None)
        if folder and os.path.isdir(folder):
            if os.name == "nt":
                os.startfile(folder)
            else:
                subprocess.Popen(["xdg-open", folder])


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        from tkinterdnd2 import TkinterDnD
        # Temporarily swap tk.Tk for TkinterDnD.Tk so AV1Compressor (which
        # subclasses tk.Tk) picks up drag-and-drop support without needing
        # a separate multiple-inheritance class.
        _OrigTk = tk.Tk
        tk.Tk = TkinterDnD.Tk
        app = AV1Compressor()
        tk.Tk = _OrigTk
    except Exception:
        app = AV1Compressor()

    app.mainloop()
