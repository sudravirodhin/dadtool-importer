"""Offline audio analysis (Phase 2). Pure audio in, metrics out — no game knowledge.

Backend-agnostic: it consumes a beat + downbeat sequence from ``tracker`` (beat_this
when configured, librosa fallback) and builds everything from it:
  - global tempo+phase via a robust line fit over all beats (minimizes drift);
  - first downbeat -> beatOffset anchor;
  - DRIFT-CRITERION sections: a segmentation of the local tempo curve is written
    only if its piecewise grid measurably lowers MAX DRIFT vs a single tempo (with
    a per-segment threshold), so accurate multi-tempo songs get sections while
    steady songs stay single-tempo;
  - confidence from how tightly the chosen model fits the beats.
Tempo floor: smallest INTEGER multiple reaching >=120 BPM.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import librosa
import numpy as np

from . import tracker

ANALYSIS_SR = 22050
HOP = 512
TEMPO_FLOOR = 120.0
TEMPO_WARN_ABOVE = 200.0
ANALYZER_VERSION = 9  # bump when analysis logic changes; invalidates the cache


@dataclass
class AnalysisResult:
    path: str
    duration_s: float
    sample_rate: int
    detected_bpm: float
    final_bpm: float
    tempo_multiplier: int
    above_200_warning: bool
    first_downbeat_s: float
    beat_period_s: float
    start_time_s: float
    end_trim_s: float
    consistency: str
    bpm_range: tuple
    bpm_sections: list
    confidence: float
    flags: list
    n_beats: int
    n_onsets: int
    coverage: float
    grid_residual_ms: float
    contrast: float
    tempo_prominence: float
    method: str = "beats"

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["bpm_range"] = list(self.bpm_range)

        def _native(v):
            if isinstance(v, np.generic):
                return v.item()
            if isinstance(v, (list, tuple)):
                return [_native(x) for x in v]
            return v

        return {k: _native(v) for k, v in d.items()}


# ---------------------------------------------------------------------------
# line fitting over beat times (global tempo/phase + drift)
# ---------------------------------------------------------------------------
def _reconstruct(seg: np.ndarray):
    """Gap-robust grid fit: round each inter-beat interval to whole grid steps so a
    skipped/added beat doesn't corrupt the index, then least-squares. Returns
    (period, phase, idx, residuals)."""
    ibi = np.diff(seg)
    p0 = float(np.median(ibi)) if ibi.size else 0.5
    if p0 <= 0:
        p0 = 0.5
    steps = np.maximum(1, np.round(ibi / p0)).astype(int)
    idx = np.concatenate([[0], np.cumsum(steps)]).astype(float)
    A = np.vstack([idx, np.ones(idx.size)]).T
    period, phase = np.linalg.lstsq(A, seg, rcond=None)[0]
    resid = seg - (phase + idx * period)
    m = np.abs(resid) < 0.4 * period
    if m.sum() > 2:
        period, phase = np.linalg.lstsq(A[m], seg[m], rcond=None)[0]
        resid = seg - (phase + idx * period)
    return float(period), float(phase), idx, resid


def fit_line(beats: np.ndarray):
    """Return (period, rms_ms, drift90_ms) for a single global tempo, gap-robust.
    drift = 90th-percentile |residual| (robust to a stray beat)."""
    period, _phase, _idx, resid = _reconstruct(beats)
    return (period, float(np.sqrt(np.mean(resid ** 2)) * 1000),
            float(np.percentile(np.abs(resid), 90) * 1000))


def clean_beats(beats: np.ndarray) -> np.ndarray:
    """Drop spurious doubled beats (interval << median) that skew the fit."""
    if beats.size < 4:
        return beats
    p0 = float(np.median(np.diff(beats)))
    if p0 <= 0:
        return beats
    kept = [float(beats[0])]
    for t in beats[1:]:
        if t - kept[-1] >= 0.6 * p0:
            kept.append(float(t))
    return np.array(kept)


def local_bpm_curve(beats: np.ndarray, win: int = 8):
    ibi = np.diff(beats)
    if ibi.size < 2:
        return beats[:1], np.array([])
    p0 = float(np.median(ibi))
    steps = np.maximum(1, np.round(ibi / p0)) if p0 > 0 else np.ones_like(ibi)
    per_beat = ibi / steps  # normalized per-beat period (robust to doubled/missed beats)
    lbpm = 60.0 / np.clip(per_beat, 1e-3, None)
    k = win // 2
    sm = np.array([np.median(lbpm[max(0, i - k):i + k + 1]) for i in range(lbpm.size)])
    return beats[:-1], sm


def section_fit_residual(beats: np.ndarray, sections: list):
    """Piecewise per-section gap-robust fit. Returns (rms_ms, maxdrift_ms)."""
    starts = [s["start_s"] for s in sections] + [1e18]
    resids = []
    for i in range(len(sections)):
        seg = beats[(beats >= starts[i]) & (beats < starts[i + 1])]
        if seg.size >= 3:
            resids.append(_reconstruct(seg)[3])
    if not resids:
        return 999.0, 999.0
    allr = np.concatenate(resids)
    return (float(np.sqrt(np.mean(allr ** 2)) * 1000),
            float(np.percentile(np.abs(allr), 90) * 1000))


def detect_sections(beats, downbeats, dev=3.0, min_run=8, improve_ms=15.0):
    """Segment the local tempo curve, then keep the segmentation only if its
    piecewise grid cuts max drift vs a single tempo by > improve_ms."""
    times, sm = local_bpm_curve(beats)
    if sm.size < min_run * 2:
        return []
    bounds, cur = [0], [sm[0]]
    for i in range(1, sm.size):
        if abs(sm[i] - np.median(cur)) > dev:
            bounds.append(i)
            cur = [sm[i]]
        else:
            cur.append(sm[i])
    segs = []
    for j, bi in enumerate(bounds):
        ei = bounds[j + 1] if j + 1 < len(bounds) else sm.size
        segs.append([bi, ei, float(np.median(sm[bi:ei]))])
    # merge short or near-equal-neighbor segments
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for i in range(len(segs)):
            bi, ei, bpm = segs[i]
            if ei - bi < min_run or (i > 0 and abs(segs[i - 1][2] - bpm) <= dev):
                j = i - 1 if i > 0 else i + 1
                a, b = min(i, j), max(i, j)
                merged = [segs[a][0], segs[b][1], float(np.median(sm[segs[a][0]:segs[b][1]]))]
                segs = segs[:a] + [merged] + segs[b + 1:]
                changed = True
                break
    # drop isolated SPIKE segments (short + far from BOTH neighbors = a tracker glitch,
    # not a real tempo change); gradual changes like an accelerando survive.
    changed = True
    while changed and len(segs) > 2:
        changed = False
        for i in range(1, len(segs) - 1):
            bi, ei, bpm = segs[i]
            left, right = segs[i - 1][2], segs[i + 1][2]
            if (ei - bi) < 1.5 * min_run and abs(bpm - left) > 0.12 * left and abs(bpm - right) > 0.12 * right:
                j = i - 1 if abs(bpm - left) < abs(bpm - right) else i + 1
                a, b = min(i, j), max(i, j)
                merged = [segs[a][0], segs[b][1], float(np.median(sm[segs[a][0]:segs[b][1]]))]
                segs = segs[:a] + [merged] + segs[b + 1:]
                changed = True
                break
    if len(segs) <= 1:
        return []
    sections = []
    for k, (bi, ei, _bpm) in enumerate(segs):
        seg_beats = beats[bi:ei + 1]
        seg_bpm = 60.0 / _reconstruct(seg_beats)[0] if seg_beats.size >= 3 else _bpm
        if k == 0:
            start_t = 0.0
        elif downbeats.size:
            start_t = float(downbeats[np.argmin(np.abs(downbeats - beats[bi]))])
        else:
            start_t = float(beats[bi])
        sections.append({"tempo": round(float(seg_bpm), 3), "start_s": round(start_t, 3)})
    _, _, md_single = fit_line(beats)
    _, md_pw = section_fit_residual(beats, sections)
    if md_single - md_pw < improve_ms:
        return []
    # Guard against fitting NOISE instead of real tempo structure: if the piecewise grid
    # still drifts badly AND barely improves on a single tempo, the "sections" are tracking
    # mis-placed beats (a song the tracker fumbled in places, e.g. dense vocals over sparse
    # percussion) rather than real changes -- prefer the robust global single tempo.
    # Calibrated on YABABAINA: its sections cut drift only ~10% and still drifted ~840 ms,
    # vs 85-97% improvement on genuinely varied songs. Deliberately conservative (needs both
    # a large residual and a tiny improvement) so only egregious mis-tracks trip it.
    if md_pw > 400.0 and md_pw > 0.8 * md_single:
        return []
    return sections


def apply_tempo_floor(bpm: float):
    if bpm <= 0:
        return bpm, 1, False
    mult = 1
    while bpm * mult < TEMPO_FLOOR:
        mult += 1
    final = bpm * mult
    return final, mult, bool(final > TEMPO_WARN_ABOVE)


def detect_silence_bounds(y, sr, rel_thresh=0.03, min_lead=1.0, min_trail=1.5, margin=0.1):
    """(start_s, end_trim_s): leading silence to skip and trailing silence to trim,
    only when the quiet stretch is prolonged (else 0). Threshold is relative to the
    track peak, so it targets true dead air, not quiet musical passages."""
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    if rms.size == 0:
        return 0.0, 0.0
    peak = float(np.max(rms))
    if peak <= 0:
        return 0.0, 0.0
    loud = np.where(rms > peak * rel_thresh)[0]
    if loud.size == 0:
        return 0.0, 0.0
    dur = len(y) / sr
    first_t = float(librosa.frames_to_time(loud[0], sr=sr, hop_length=512))
    last_t = float(librosa.frames_to_time(loud[-1], sr=sr, hop_length=512))
    start_s = max(0.0, first_t - margin) if first_t > min_lead else 0.0
    trail = dur - last_t
    end_trim = max(0.0, trail - margin) if trail > min_trail else 0.0
    return float(start_s), float(end_trim)


def compute_confidence(residual_ms, n_beats, source):
    if residual_ms <= 12:
        base = 0.92
    elif residual_ms <= 22:
        base = 0.82
    elif residual_ms <= 35:
        base = 0.65
    elif residual_ms <= 55:
        base = 0.45
    else:
        base = 0.25
    if n_beats < 40:
        base *= 0.7
    if source == "beat_this":
        base = min(1.0, base + 0.05)
    return float(np.clip(base, 0, 1))


def _empty_result(path, duration, sr, flags, source):
    return AnalysisResult(
        path=path, duration_s=round(duration, 3), sample_rate=sr, detected_bpm=0.0,
        final_bpm=0.0, tempo_multiplier=1, above_200_warning=False, first_downbeat_s=0.0,
        beat_period_s=0.0, start_time_s=0.0, end_trim_s=0.0, consistency="CONSISTENT", bpm_range=(0.0, 0.0),
        bpm_sections=[], confidence=0.0, flags=flags, n_beats=0, n_onsets=0, coverage=0.0,
        grid_residual_ms=0.0, contrast=0.0, tempo_prominence=0.0, method=source,
    )


def analyze(path: str) -> AnalysisResult:
    info = tracker.get_beats(path)
    beats = clean_beats(np.array(sorted(float(b) for b in info.get("beats", []))))
    downbeats = np.array(sorted(float(d) for d in info.get("downbeats", [])))
    source = info.get("source", "?")
    flags: list[str] = []

    y, sr = librosa.load(path, sr=ANALYSIS_SR, mono=True)
    duration = len(y) / sr
    start_s, end_trim_s = detect_silence_bounds(y, sr)

    if beats.size < 8:
        flags.append("too few beats - very low confidence")
        return _empty_result(path, duration, sr, flags, source)

    period, rms_single, maxdrift_single = fit_line(beats)
    global_bpm = 60.0 / period if period > 0 else 0.0

    db = downbeats[downbeats >= start_s - 0.05]
    if db.size:
        first_db = float(db[0])
    else:
        after = beats[beats >= start_s - 0.05]
        first_db = float(after[0]) if after.size else float(beats[0])

    final_bpm, mult, warn = apply_tempo_floor(global_bpm)

    sections = detect_sections(beats, downbeats)
    if sections:
        consistency = "VARIABLE"
        rms_final, maxdrift_final = section_fit_residual(beats, sections)
        # Floor each section INDEPENDENTLY: a dynamic shift the tracker caught at
        # half-time (e.g. an 87-BPM bridge inside a 175-BPM song) gets doubled until it
        # clears 120 so it stops crawling, while sections already in range are left
        # alone. A single song-wide multiplier can't do this -- a fast song's global
        # tempo is >=120 (multiplier 1), so its half-time sections would never get
        # lifted. (User: "the dynamic BPM shifts should also be doubled.")
        # The smallest integer multiple is used even when it overshoots 200: a uniform
        # 2x is preferable to leaving a section oddly slow (user's call -- some songs
        # will have very fast segments, and that's fine).
        doubled = []
        for s in sections:
            m = 1
            while s["tempo"] * m < TEMPO_FLOOR:
                m += 1
            if m > 1:
                doubled.append((round(s["tempo"], 1), m))
            s["tempo"] = round(s["tempo"] * m, 3)
        seg_bpms = [s["tempo"] for s in sections]
        bpm_range = (min(seg_bpms), max(seg_bpms))
        note = f"; lifted {len(doubled)} slow shift(s)" if doubled else ""
        flags.append(f"VARIABLE: {len(sections)} sections {bpm_range[0]:.0f}-{bpm_range[1]:.0f} BPM "
                     f"(per-section floor{note}; drift {maxdrift_single:.0f}->{maxdrift_final:.0f} ms)")
        if max(seg_bpms) > 205:
            flags.append("some section tempos >200 - review in-game")
    else:
        consistency = "CONSISTENT"
        rms_final = rms_single
        _, sm = local_bpm_curve(beats)
        bpm_range = (float(np.min(sm)), float(np.max(sm))) if sm.size else (global_bpm, global_bpm)

    if mult > 1:
        flags.append(f"detected {global_bpm:.2f} < {TEMPO_FLOOR:.0f}; applied x{mult} -> {final_bpm:.2f}")
    if warn:
        flags.append(f"WARNING: final tempo {final_bpm:.1f} BPM > 200 - difficult to play")

    confidence = compute_confidence(rms_final, int(beats.size), source)
    if confidence < 0.5:
        flags.append("low confidence - preview / review recommended")

    return AnalysisResult(
        path=path, duration_s=round(duration, 3), sample_rate=sr,
        detected_bpm=round(global_bpm, 4), final_bpm=round(final_bpm, 4),
        tempo_multiplier=mult, above_200_warning=warn, first_downbeat_s=round(first_db, 4),
        beat_period_s=round(period, 6), start_time_s=round(start_s, 3),
        end_trim_s=round(end_trim_s, 3), consistency=consistency,
        bpm_range=(round(bpm_range[0], 2), round(bpm_range[1], 2)), bpm_sections=sections,
        confidence=round(confidence, 3), flags=flags, n_beats=int(beats.size),
        n_onsets=int(beats.size), coverage=0.0, grid_residual_ms=round(rms_final, 2),
        contrast=0.0, tempo_prominence=0.0, method=source,
    )
