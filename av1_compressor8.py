===FILE: av1_compressor8.py
```
#!/usr/bin/env python3
"""
AV1 Video Compressor
Drag & drop videos to batch re-encode with av1_nvenc for Google Photos.
Preserves original creation_time metadata so Google Photos keeps correct date ordering.
"""

import sys
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton, QComboBox, QLineEdit, QCheckBox, QTreeView, QTableWidget, QTableWidgetItem, QPlainTextEdit, QFileDialog, QMessageBox, QAction, QStatusBar
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QTextCursor, QIcon
import subprocess
import threading
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

# ── Denoise (CPU) ────────────────────────────────────────────────────────
# Reverted from GPU/OpenCL (nlmeans_opencl) back to hqdn3d after repeated
# "Invalid output format ... for hwframe download" failures on this
# machine's NVIDIA OpenCL ICD — the pixel-format negotiation between
# hwupload/nlmeans_opencl/hwdownload never resolved cleanly. hqdn3d is
# CPU-only but reliable and fast (single spatial+light temporal pass).
#
# Light: mild settings, minimal detail loss.
# Medium: ffmpeg's own hqdn3d defaults (luma_spatial:chroma_spatial:luma_tmp:chroma_tmp).
DENOISE_FILTERS = {
    "light":  "hqdn3d=1.5:1.5:3:3",
    "medium": "hqdn3d=4:3:6:4.5",
}

# Effect on estimated output size: denoising removes grain/sensor noise,
# so the encoder needs fewer bits to hit the same perceptual quality at a
# given CQ. Medium removes more noise than light, so it saves a bit more.
DENOISE_BPP_FACTOR = {
    "off":    1.0,
    "light":  0.90,
    "medium": 0.82,
}

# ── Size-estimate calibration ────────────────────────────────────────────
# CQ on av1_nvenc is *constant-QP* rate control, not a software CRF curve.
# Real-world testing shows NVENC's constqp mode allocates noticeably more
# bits per quality step than software encoders (SVT-AV1/x265-style CRF),
# so a plain "halve bits every +6 CQ" model consistently underestimates.
#
# Calibration point from real-world feedback: estimated ~900 MB, actual
# output was ~2.15 GB (ratio ≈ 2.45x). BASE_BPP_AT_CQ32 and
# CALIBRATION_FACTOR below are tuned to that data point. If your results
# keep drifting high/low by a consistent ratio, adjust CALIBRATION_FACTOR
# — it's a single multiplier applied to the video-bitrate estimate only.
BASE_BPP_AT_CQ32 = 0.045
CALIBRATION_FACTOR = 2.45

PRESET_SIZE_FACTOR = {
    "p1": 1.15, "p2": 1.10, "p3": 1.07, "p4": 1.03,
    "p5": 1.00, "p6": 0.97, "p7": 0.93,
}

AUDIO_BITRATES = {
    "aac 128k": 128_000,
    "aac 256k": 256_00