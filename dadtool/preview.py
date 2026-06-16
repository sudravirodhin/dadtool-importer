"""Offline click-track preview (QoL #7, and the main analyzer validation tool).

Render the song with a metronome on the DETECTED grid so sync can be ear-checked
in ~10 s without launching the game. Clicks are placed on the FINAL grid (after
the integer tempo floor) so you hear exactly what the game will play; musical
downbeats get a higher-pitched accent.
"""
from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np
import soundfile as sf


def make_click_preview(
    audio_path: str,
    out_path: str,
    final_period_s: float,
    first_downbeat_s: float,
    bar_period_s: float | None = None,
    start_time_s: float = 0.0,
    seconds: float | None = 30.0,
) -> tuple[str, int, float]:
    """Write a song+metronome WAV. Returns (out_path, sr, rendered_seconds)."""
    y, sr = librosa.load(audio_path, sr=None, mono=True)
    total = len(y) / sr

    # window: start a touch before the first downbeat so the count-in is audible
    t0 = max(0.0, min(first_downbeat_s, start_time_s) - 0.5)
    if seconds:
        i0, i1 = int(t0 * sr), min(len(y), int((t0 + seconds) * sr))
    else:
        i0, i1 = int(t0 * sr), len(y)
    yseg = y[i0:i1]
    segdur = len(yseg) / sr

    beat_phase = first_downbeat_s % final_period_s
    grid = np.arange(beat_phase, total, final_period_s)
    grid_seg = grid[(grid >= t0) & (grid < t0 + segdur)] - t0
    beat_click = librosa.clicks(times=grid_seg, sr=sr, length=len(yseg), click_freq=1200.0)

    db_click = np.zeros_like(yseg)
    if bar_period_s:
        db_phase = first_downbeat_s % bar_period_s
        db = np.arange(db_phase, total, bar_period_s)
        db_seg = db[(db >= t0) & (db < t0 + segdur)] - t0
        db_click = librosa.clicks(times=db_seg, sr=sr, length=len(yseg), click_freq=2000.0)

    music = yseg / (np.max(np.abs(yseg)) + 1e-9) * 0.6
    mix = music + 0.9 * beat_click + 1.0 * db_click
    mix = mix / (np.max(np.abs(mix)) + 1e-9) * 0.95

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(out_path, mix, sr)
    return out_path, sr, round(segdur, 2)
