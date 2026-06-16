"""LUFS loudness normalization via ffmpeg's two-pass `loudnorm` (EBU R128).

Touches audio only (never game internals): produces a normalized 48 kHz stereo
Ogg Vorbis so volume is consistent across the library. Two-pass = measure, then
encode with the measured values (accurate + true-peak limited, no clipping).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

DEFAULT_I = -14.0    # integrated LUFS target (streaming-ish)
DEFAULT_TP = -1.5    # true-peak ceiling (dBTP)
DEFAULT_LRA = 11.0   # loudness range


def _ff() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def measure(path, target_i=DEFAULT_I, target_tp=DEFAULT_TP, lra=DEFAULT_LRA) -> dict:
    """Pass 1: loudnorm analysis. Returns the measured stats dict."""
    af = f"loudnorm=I={target_i}:TP={target_tp}:LRA={lra}:print_format=json"
    p = subprocess.run([_ff(), "-hide_banner", "-i", str(path), "-af", af, "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    blocks = [b for b in re.findall(r"\{[^{}]+\}", p.stderr) if "input_i" in b]
    if not blocks:
        raise RuntimeError("loudnorm measure failed: " + (p.stderr or "")[-300:])
    return json.loads(blocks[-1])


def loudnorm_to_ogg(src, dest, target_i=DEFAULT_I, target_tp=DEFAULT_TP, lra=DEFAULT_LRA, sr=48000) -> float:
    """Two-pass normalize `src` to a 48 kHz stereo Vorbis ogg at the target LUFS.
    Returns the measured input integrated loudness."""
    st = measure(src, target_i, target_tp, lra)
    af = (f"loudnorm=I={target_i}:TP={target_tp}:LRA={lra}"
          f":measured_I={st['input_i']}:measured_TP={st['input_tp']}"
          f":measured_LRA={st['input_lra']}:measured_thresh={st['input_thresh']}"
          f":offset={st['target_offset']}:linear=true")
    p = subprocess.run(
        [_ff(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vn", "-af", af, "-ar", str(sr), "-ac", "2", "-c:a", "libvorbis", "-q:a", "5", str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if p.returncode != 0 or not Path(dest).exists():
        raise RuntimeError("loudnorm encode failed: " + (p.stderr or "")[-300:])
    return float(st["input_i"])
