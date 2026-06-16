"""Phase 3 writer: map analyzer output into the game's Meta.json.

Thin adapter over FORMAT.md, kept isolated so a format change only breaks this
file. Field mapping (units confirmed in Phase 1):
  tempo               <- final_bpm  (float32; whole numbers as int, like the game)
  beatOffset (ms int) <- (first_downbeat_s mod final_beat_period) * 1000
  startSongOffset (s) <- leading-silence trim (if > 0.5 s)
  customTempoSections <- [] for CONSISTENT (single global tempo)
All other fields (uniqueId, seed, songName, credits...) are preserved untouched.

Safety gates before any write: game-not-running, version unchanged (else demand
Phase 1 revalidation), format canary on a known song, full Saved backup, and
verify-by-reread.
"""
from __future__ import annotations

import struct
from pathlib import Path

from . import backup, gamestate, meta, paths

# expected Meta.json schema (key -> acceptable python types) per FORMAT.md
REQUIRED_KEYS = {
    "version": (int,),
    "uniqueId": (int,),
    "tempo": (int, float),
    "customTempoSections": (list,),
    "beatOffset": (int,),
    "startSongOffset": (int, float),
    "endSongOffset": (int, float),
    "uEAssetName": (str,),
}

CANARY_SONG = "Overkill (Acoustic Version) - Colin Hay"
TEMPO_FLOOR = 120.0  # keep in sync with analyzer.TEMPO_FLOOR


def to_float32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


def tempo_json_value(bpm: float):
    """Match the game: whole tempos as int, else the float32 value."""
    f = to_float32(bpm)
    return int(round(f)) if abs(f - round(f)) < 1e-6 else f


def beat_offset_ms(first_downbeat_s: float, final_bpm: float) -> int:
    period = 60.0 / final_bpm
    return int(round((first_downbeat_s % period) * 1000.0))


def build_meta(existing: dict, result=None, overrides: dict | None = None) -> dict:
    """Return a new meta dict: a copy of `existing` with sync fields updated."""
    overrides = overrides or {}
    data = dict(existing)

    # heal originalAudioFilePath if the source was relocated into audio/processed/
    oap = data.get("originalAudioFilePath")
    if oap:
        base = oap.replace("\\", "/").rsplit("/", 1)[-1]
        cand = paths.PROCESSED_DIR / base
        if cand.exists() and not Path(oap).exists():
            data["originalAudioFilePath"] = str(cand).replace("\\", "/")

    if "tempo" in overrides:
        final_bpm = float(overrides["tempo"])
    elif result is not None:
        final_bpm = float(result.final_bpm)
    else:
        final_bpm = float(existing.get("tempo", 120))
    data["tempo"] = tempo_json_value(final_bpm)

    if "beatOffset" in overrides:
        data["beatOffset"] = int(overrides["beatOffset"])
    elif result is not None:
        data["beatOffset"] = beat_offset_ms(result.first_downbeat_s, final_bpm)

    if "customTempoSections" in overrides:
        data["customTempoSections"] = overrides["customTempoSections"]
    elif result is not None:
        secs = getattr(result, "bpm_sections", None) or []
        if secs and "tempo" not in overrides:
            # section tempos are already floored by the analyzer (duration-dominant)
            data["customTempoSections"] = [
                {"tempo": tempo_json_value(s["tempo"]), "startAbsoluteTime": float(s["start_s"])}
                for s in secs
            ]
            data["tempo"] = tempo_json_value(secs[0]["tempo"])
            if "beatOffset" not in overrides:
                data["beatOffset"] = beat_offset_ms(result.first_downbeat_s, secs[0]["tempo"])
        # else: no detected sections -> PRESERVE existing customTempoSections
        # (the song's current value, e.g. sections you added in-game), never wipe.

    if "startSongOffset" in overrides:
        data["startSongOffset"] = overrides["startSongOffset"]
    elif result is not None:
        data["startSongOffset"] = round(float(result.start_time_s), 3)

    if "endSongOffset" in overrides:
        data["endSongOffset"] = overrides["endSongOffset"]
    elif result is not None:
        data["endSongOffset"] = round(float(getattr(result, "end_trim_s", 0.0)), 3)

    return data


# ---------------------------------------------------------------------------
# safety gates
# ---------------------------------------------------------------------------
def version_status() -> tuple[bool, str]:
    cfg = paths.load_config()
    baseline = (cfg.get("version_baseline") or {}).get("steam_build_id")
    live = gamestate.detect_version()
    val = live.get("value")
    if baseline and val and str(val) != str(baseline):
        return False, f"game build changed {baseline} -> {val}: re-run Phase 1 revalidation"
    return True, f"version {val} (baseline {baseline})"


def canary_status(reference_song: str = CANARY_SONG) -> tuple[bool, str]:
    mj = paths.imported_songs_dir() / reference_song / "Meta.json"
    if not mj.exists():
        return True, f"canary song missing ({reference_song}); skipped"
    try:
        d, _ = meta.read_meta(mj)
    except Exception as e:  # noqa: BLE001
        return False, f"canary parse failed: {e}"
    for k, types in REQUIRED_KEYS.items():
        if k not in d:
            return False, f"canary: missing key '{k}' (format changed?)"
        if not isinstance(d[k], types):
            return False, f"canary: key '{k}' has unexpected type {type(d[k]).__name__}"
    return True, "canary ok"


def preflight(allow_running: bool = False) -> tuple[bool, list[str]]:
    msgs = []
    running = gamestate.is_game_running()
    if running:
        if not allow_running:
            return False, [f"game is running ({', '.join(running)}); refusing to write"]
        msgs.append(f"WARNING: game is running ({', '.join(running)}) - HOT write")
    vok, vmsg = version_status()
    msgs.append(vmsg)
    if not vok:
        return False, msgs
    cok, cmsg = canary_status()
    msgs.append(cmsg)
    if not cok:
        return False, msgs
    return True, msgs


# ---------------------------------------------------------------------------
def write_song(song: str, result=None, overrides: dict | None = None,
               do_backup: bool = True, allow_running: bool = False) -> dict:
    ok, msgs = preflight(allow_running=allow_running)
    if not ok:
        raise RuntimeError("; ".join(msgs))

    mj = paths.imported_songs_dir() / song / "Meta.json"
    if not mj.exists():
        raise FileNotFoundError(f"Meta.json not found for '{song}'")
    existing, enc = meta.read_meta(mj)
    new = build_meta(existing, result, overrides)

    backup_dir = backup.backup_saved(reason=f"write {song}") if do_backup else None
    meta.write_meta(mj, new, enc)

    back, enc2 = meta.read_meta(mj)
    fields = ("tempo", "beatOffset", "customTempoSections", "startSongOffset")
    verified = (enc2 == enc) and all(back.get(k) == new.get(k) for k in fields)
    return {
        "song": song,
        "verified": verified,
        "encoding": enc2,
        "written": {k: new.get(k) for k in fields},
        "previous": {k: existing.get(k) for k in fields},
        "backup": str(backup_dir) if backup_dir else None,
        "preflight": msgs,
    }
