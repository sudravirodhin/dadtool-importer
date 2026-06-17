"""Beat/downbeat tracking backends.

Primary: beat_this (SOTA transformer, 2024) in an isolated conda env, called as a
subprocess (configured via ``beat_this_python`` in dad_config.json). Fallback:
librosa in-process. Both return absolute beat + downbeat times in seconds, so the
analyzer downstream is backend-agnostic.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np

from . import paths

WORKER = paths.REPO_ROOT / "scripts" / "beat_this_worker.py"
BEAT_CACHE = paths.CACHE_DIR / "beat_cache.json"  # raw tracker output, keyed by audio hash


def _beat_this_python() -> str | None:
    p = paths.load_config().get("beat_this_python")
    return p if p and Path(p).exists() else None


def beats_from_beat_this(audio_path, timeout: int = 900) -> dict | None:
    py = _beat_this_python()
    if not py or not WORKER.exists():
        return None
    try:
        out = subprocess.run(
            [py, str(WORKER), str(audio_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None
    if "beats" not in data:
        return None
    data["source"] = "beat_this"
    return data


def _downbeats_4_4(oenv, sr, hop, beats, bpb: int = 4) -> list[float]:
    import librosa
    if len(beats) < bpb:
        return [float(beats[0])] if len(beats) else []
    frames = np.clip(librosa.time_to_frames(np.asarray(beats), sr=sr, hop_length=hop), 0, len(oenv) - 1)
    strengths = oenv[frames]
    scores = [float(np.mean(strengths[m::bpb])) for m in range(bpb)]
    ph = int(np.argmax(scores))
    return [float(beats[i]) for i in range(ph, len(beats), bpb)]


def beats_from_librosa(audio_path) -> dict:
    import librosa
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
    bpm, beat_frames = librosa.beat.beat_track(onset_envelope=oenv, sr=sr, hop_length=512)
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=512).tolist()
    return {
        "beats": beats,
        "downbeats": _downbeats_4_4(oenv, sr, 512, beats),
        "bpm_median": float(np.atleast_1d(bpm)[0]),
        "source": "librosa",
    }


def _audio_key(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:24]


def _load_beat_cache() -> dict:
    if BEAT_CACHE.exists():
        try:
            return json.loads(BEAT_CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def get_beats(audio_path, use_cache: bool = True) -> dict:
    """beat_this if configured + reachable, else librosa. Raw output is cached by
    audio-content hash, so the neural model runs at most once per song even across
    analyzer-logic changes."""
    key = None
    cache = None
    if use_cache:
        try:
            key = _audio_key(audio_path)
        except OSError:
            key = None
        if key:
            cache = _load_beat_cache()
            cached = cache.get(key)
            if cached:
                return cached
    result = beats_from_beat_this(audio_path) or beats_from_librosa(audio_path)
    if key and result:
        if cache is None:
            cache = _load_beat_cache()
        cache[key] = result
        paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        BEAT_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return result
