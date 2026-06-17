r"""Challenge generator -- PLANNER stage.

Lays out enemy waves to fill a song's length plus a 2-wave buffer, scaling each wave's
size to the song's spectral intensity at that point. The clear-time estimate is the
user-calibrated model:

    weight(enemy) = regular 1x, Bouncer 2.5x, boss 5x      (most regulars die equally fast)
    wave_clear_s  = BASE_CLEAR_S * sum(weights) / tempo_factor   (faster song -> faster clears)

Waves are generated until the cumulative optimal-clear time covers the song, then padded
with LEEWAY_WAVES extra waves so a flawless/fast run never runs dry before the song ends.
This stage plans in ABSTRACT enemy categories; mapping to concrete enemy tags + writing the
UserChallenges JSON happens once the placeable-enemy vocabulary is confirmed.
"""
from __future__ import annotations

import numpy as np

from . import backup, gamestate, meta, paths

WEIGHTS = {"regular": 1.0, "bouncer": 2.5, "boss": 5.0}

# Tags sourced from challenge_vocab.json (harvested from game content)
# Confirmed placeable enemy tags (from sample challenges). Regulars share weight 1.0;
# the dict values are how the wave's regulars are split across types (for variety).
REGULAR_TAGS = {
    "Entity.Character.Enemy.Grunt.Mosher": 0.50,
    "Entity.Character.Enemy.Grunt.Stan.Default": 0.20,
    "Entity.Character.Enemy.Grunt.Echo": 0.10,
    "Entity.Character.Enemy.Guard.Shield": 0.12,
    "Entity.Character.Enemy.Guard.Baton": 0.08,
}
BOUNCER_TAG = "Entity.Character.Enemy.Boss.Bouncer"
BOSS_TAGS = ["Entity.Character.Enemy.Boss.Rebel", "Entity.Character.Enemy.Boss.Doll",
             "Entity.Character.Enemy.Boss.Shred", "Entity.Character.Enemy.Boss.Prophet"]
def _mod(folder, name):
    return f"/Game/Pagoda/DataMod/{folder}/DA_DataMod_{name}.DA_DataMod_{name}"


# Mods with folders confirmed from sample challenges + the pak DataMod scan.
HINDER_MODS = {  # make it harder
    "EnemyIncreasedHealth": _mod("Hinder", "EnemyIncreasedHealth"),
    "DoubleTime": _mod("Hinder", "DoubleTime"),
    "DecreasedHealthPickupChance": _mod("Hinder", "DecreasedHealthPickupChance"),
}
HELP_MODS = {  # make it easier (player buffs)
    "IncreasedFeverRegen_50Percent": _mod("Help", "IncreasedFeverRegen_50Percent"),
    "PlayerHealthRegen": _mod("Help", "PlayerHealthRegen"),
    "AddFeverBar": _mod("Help", "AddFeverBar"),
}
# Arenas (tags confirmed from challenges + the DL_Arena/arena_tag pak scan), ordered
# roughly calm -> intense so the stage tracks the song's vibe.
ARENAS = [
    "Environment.Arena.Idol.Doll.WaterArena",
    "Environment.Arena.Idol.Prophet.RecordingStudio",
    "Environment.Arena.Idol.Prophet.ParkingLot",
    "Environment.Arena.Idol.Rebel.Platform",
    "Environment.Arena.Idol.Prophet.ColosseumHall",
    "Environment.Arena.Idol.Shred.Reactor",
]
DEFAULT_ARENA = ARENAS[0]
BASE_CLEAR_S = 2.0        # seconds to clear one regular at REF_TEMPO; TUNE by one playtest
REF_TEMPO = 130.0
LEEWAY_WAVES = 2
SECS_PER_WAVE = 8.5       # validated average wave duration
MIN_WAVES = 8
MAX_WAVES = 24
DIFF_SCALE = {"easy": 0.6, "normal": 1.0, "hard": 1.5}


def profile(folder: str) -> dict:
    """Spectral + tempo profile for an imported song folder."""
    import librosa
    base = paths.imported_songs_dir() / folder
    data, _ = meta.read_meta(base / "Meta.json")
    tempo = float(data.get("tempo") or REF_TEMPO)
    y, sr = librosa.load(str(base / "Audio.ogg"), sr=22050, mono=True)
    audio_dur = len(y) / sr
    dur = max(30.0, audio_dur - float(data.get("startSongOffset") or 0) - float(data.get("endSongOffset") or 0))
    rms = librosa.feature.rms(y=y)[0].astype(float)
    if rms.max() > 0:
        rms /= rms.max()
    k = max(1, len(rms) // 100)
    curve = np.convolve(rms, np.ones(k) / k, mode="same")
    return {"folder": folder, "name": data.get("songName", folder), "tempo": tempo,
            "duration": dur, "curve": curve, "overall": float(np.mean(curve))}


def _intensity_at(curve, frac: float) -> float:
    if len(curve) == 0:
        return 0.5
    return float(curve[min(len(curve) - 1, max(0, int(frac * len(curve))))])


ALL_MODS = {**HINDER_MODS, **HELP_MODS}


def _select_mods(prof: dict, scale: float) -> list[str]:
    """Pick modifier KEYS by vibe (mapped to asset paths at emit time)."""
    mods = []
    inten = prof["overall"]
    if scale >= 1.25 or inten > 0.5:
        mods.append("EnemyIncreasedHealth")
    if prof["tempo"] >= 175 and scale >= 1.0:
        mods.append("DoubleTime")
    if scale <= 0.75 and inten < 0.4:
        mods.append("IncreasedFeverRegen_50Percent")
    return mods


def _select_arena(prof: dict) -> str:
    """Stage by vibe: calmer songs -> serene arenas, intense -> aggressive ones."""
    i = round((prof["overall"] - 0.30) / 0.20 * (len(ARENAS) - 1))
    return ARENAS[max(0, min(len(ARENAS) - 1, i))]


def _boss_count(scale: float) -> int:
    """Bosses (5x) scale with the song's vibe; the most brutal songs stack them."""
    if scale >= 1.5:
        return 3
    if scale >= 1.25:
        return 2
    if scale >= 1.0:
        return 1
    return 0


def auto_name(song_name: str) -> str:
    """Canonical name for an auto-generated challenge ('Auto - ' marks it for sync --purge)."""
    import re
    n = re.sub(r'[<>:"/\\|?*]', "", f"Auto - {song_name}").strip()
    return n or "Auto Challenge"


AUTO_PREFIX = "Auto - "


def plan(prof: dict, difficulty: str = "auto") -> dict:
    if difficulty == "auto":
        scale = 0.7 + prof["overall"] * 0.9          # ~0.7 (calm) .. ~1.6 (intense)
    else:
        scale = DIFF_SCALE.get(difficulty, 1.0)
    tf = prof["tempo"] / REF_TEMPO
    dur = prof["duration"]
    peak = float(np.max(prof["curve"])) if len(prof["curve"]) else 1.0
    target = max(MIN_WAVES, min(MAX_WAVES, round(dur / SECS_PER_WAVE)))   # bounded wave count
    base_units = max(2.0, (dur / target) * tf / BASE_CLEAR_S)  # regulars/wave to fill song at normal
    waves = []
    for i in range(target):
        frac = (i + 0.5) / target
        inten = _intensity_at(prof["curve"], frac)
        rel = inten / peak if peak else 0.0          # intensity relative to this song's peak
        units = base_units * (0.6 + inten * 0.8) * scale             # 3..14 regulars-worth, scaled
        n_reg = max(2, round(units))
        # specials show up in this song's more intense moments (calibrated weight 2.5x)
        n_bnc = (1 if rel >= 0.7 and scale >= 0.9 else 0) + (1 if rel >= 0.92 and scale >= 1.2 else 0)
        weight = n_reg * WEIGHTS["regular"] + n_bnc * WEIGHTS["bouncer"]
        clear = BASE_CLEAR_S * weight / tf
        waves.append({"i": len(waves) + 1, "at": round(frac * dur), "intensity": round(inten, 2),
                      "regular": n_reg, "bouncer": n_bnc, "weight": round(weight, 1),
                      "clear_s": round(clear, 1), "leeway": False})
    peak = max(waves, key=lambda w: w["weight"]) if waves else None
    for _ in range(LEEWAY_WAVES):
        if peak:
            waves.append({**peak, "i": len(waves) + 1, "leeway": True})
    return {"name": prof["name"], "waves": waves, "mods": _select_mods(prof, scale),
            "scale": round(scale, 2), "tempo": prof["tempo"], "tempo_factor": round(tf, 2),
            "song_s": round(dur, 1), "est_total_s": round(sum(w["clear_s"] for w in waves), 1),
            "overall_intensity": round(prof["overall"], 2)}


# --------------------------------------------------------------------------- emitter
def _distribute_regulars(n: int) -> dict:
    """Split n regular enemies across the regular types by their variety weights."""
    items = sorted(REGULAR_TAGS.items(), key=lambda kv: -kv[1])
    out, assigned = {}, 0
    for tag, w in items:
        c = int(round(n * w))
        out[tag] = c
        assigned += c
    out[items[0][0]] += (n - assigned)  # remainder -> the most common type
    return {t: c for t, c in out.items() if c > 0}


def build_challenge(folder: str, name: str, difficulty: str = "auto",
                    arena: str | None = None, boss: str = "auto") -> tuple[dict, dict]:
    """Build a Challenge dict (and its plan) for an imported song folder. arena=None picks
    by vibe; boss in {"auto","force","none"} (auto = vibe-scaled stacking)."""
    prof = profile(folder)
    song_meta, _ = meta.read_meta(paths.imported_songs_dir() / folder / "Meta.json")
    pl = plan(prof, difficulty)
    arena = arena or _select_arena(prof)
    n_boss = 0 if boss == "none" else _boss_count(pl["scale"]) + (2 if boss == "force" else 0)
    real = [i for i, w in enumerate(pl["waves"]) if not w["leeway"]]
    last_real = real[-1] if real else -1
    waves = []
    for i, w in enumerate(pl["waves"]):
        counts = _distribute_regulars(w["regular"])
        if w["bouncer"]:
            counts[BOUNCER_TAG] = counts.get(BOUNCER_TAG, 0) + w["bouncer"]
        if n_boss and i == last_real:  # stack the bosses in the finale wave
            for b in range(n_boss):
                bt = BOSS_TAGS[b % len(BOSS_TAGS)]
                counts[bt] = counts.get(bt, 0) + 1
        npc = {f'(TagName="{t}")': c for t, c in counts.items() if c > 0}
        waves.append({"nPCCounts": npc, "bMaintainSpawnCount": False, "patternData": "None"})
    challenge = {
        "challengeName": name,
        "songMetadata": song_meta,
        "enemyWaves": waves,
        "objectives": [{"objectiveDefTag": {"tagName": "PagodaObjectives.WinOnSongEnd"},
                        "metadataAsJson": ""}],
        "modAssets": [ALL_MODS[k] for k in pl["mods"] if k in ALL_MODS],
        "arenaIdTag": {"tagName": arena},
        "arenaColorIndex": 0,
    }
    pl["arena"], pl["n_boss"] = arena, n_boss
    return challenge, pl


def write_challenge(name: str, challenge: dict, *, do_backup: bool = True,
                    allow_running: bool = False) -> dict:
    if not allow_running:
        procs = gamestate.is_game_running()
        if procs:
            raise RuntimeError(f"game is running ({', '.join(procs)}); refusing to write")
    if do_backup:
        backup.backup_saved(reason=f"challenge '{name}'")
    dest = paths.saved_dir() / "UserChallenges" / name / "Meta.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta.write_meta(dest, challenge, "utf-8")  # "utf-8" base; write_meta promotes to UTF-16+BOM if non-ASCII content
    verified = meta.read_meta(dest)[0].get("challengeName") == name
    return {"name": name, "file": str(dest), "waves": len(challenge["enemyWaves"]),
            "verified": verified}
