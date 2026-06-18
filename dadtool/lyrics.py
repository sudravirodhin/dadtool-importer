r"""Audio -> timed .lrc lyrics for the Marquee mod's cache (producer side).

The mod is a pure CONSUMER: it reads ``<key>.lrc`` from its cache dir at runtime. This
module is the producer. ASR runs in an ISOLATED venv (faster-whisper, no torch) via
scripts/lrcgen_worker.py, invoked as a subprocess (configured by ``lrcgen_python`` in
dad_config.json) -- exactly like beat_this in tracker.py.

dadtool owns the TIMING CONVENTION: the worker emits file-relative timestamps and this
module applies ``lyrics_timing_model`` (it already has each song's startSongOffset):
  file                  -> timestamps == file time, no shift   (game plays from t=0)
  subtract-start-offset -> timestamp = file_time - startSongOffset (game starts at offset)

Files written into the cache dir, keyed by the song's stable ``uniqueId``:
  <key>.lrc        timed lyrics the mod reads (UTF-8, NO BOM -- a BOM breaks [ti:])
  <key>.txt        plain transcript for the human proof pass
  <key>.words.json per-word timings (future karaoke; the mod ignores word stamps now)
  <key>.miss       written INSTEAD, for instrumentals (mod stops re-querying)

Output is a DRAFT: ASR mishears sung lyrics, so it is for proofing, never silently final.
Lyrics are generated per-user at import time; never bundle them in mod releases.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import gamestate, meta, paths

WORKER = paths.REPO_ROOT / "scripts" / "lrcgen_worker.py"
MODELS_DIR = paths.REPO_ROOT / "lyrics_lab" / "models"        # cached large-v3 lives here
BACKUP_DIR = paths.REPO_ROOT / "lyrics_lab" / "backup_existing"

BY_TAG = "dadtool ASR (faster-whisper large-v3) - DRAFT, proof the .txt"


def _cfg() -> dict:
    return paths.load_config()


def cache_dir() -> Path:
    """Resolve the Marquee mod's lyrics cache directory.

    Priority: explicit ``lyrics_cache_dir`` config key → derived from
    ``game_install_dir`` → raise with actionable message.
    """
    cfg = _cfg()
    # Explicit override takes priority
    explicit = cfg.get("lyrics_cache_dir")
    if explicit:
        return Path(explicit)
    # Derive from game_install_dir if available
    install = cfg.get("game_install_dir")
    if install:
        derived = (Path(install) / "Pagoda" / "Binaries" / "Win64"
                   / "ue4ss" / "Mods" / "Marquee" / "Scripts" / "data" / "lyrics")
        return derived
    raise RuntimeError(
        "lyrics cache dir unknown: set 'lyrics_cache_dir' or 'game_install_dir' "
        "in dad_config.json (see dad_config.example.json)")


def mod_installed(out=None) -> bool:
    """True when the Marquee mod's lyrics dir is available (Marquee is a SISTER mod; we
    only write when it's installed -- e.g. the game is installed and the mod is present).
    We never fabricate a fake mod tree: the dir must exist, or its parent (the mod's
    .../Scripts/data) must already exist so creating the leaf 'lyrics' folder is correct.
    A custom out dir is treated the same way (its parent must exist)."""
    d = Path(out) if out else cache_dir()
    return d.exists() or d.parent.exists()


def purge_cache(out_dir=None) -> dict:
    """Back up the ENTIRE lyrics cache, then delete every generated lyric file (.lrc/.miss/
    .offset/.txt/.words.json) so a regenerate starts clean. KEEPS .gitkeep, _requests.jsonl, and _catalog.jsonl
    (the latter is the mod's built-in queue, needed by `--queue` after a purge). For a
    deliberate full rebuild: the old .offset nudges are cleared too (they were calibrated to
    the old, possibly mistimed lyrics) -- the full backup preserves them if needed."""
    import shutil
    out = Path(out_dir) if out_dir else cache_dir()
    if not out.exists():
        return {"backup": None, "removed": 0}
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"lyrics_cache_{ts}"
    shutil.copytree(out, backup)
    removed = 0
    for p in out.iterdir():
        if p.is_file() and p.name not in (".gitkeep", "_requests.jsonl", "_catalog.jsonl"):
            p.unlink()
            removed += 1
    return {"backup": str(backup), "removed": removed}


# --------------------------------------------------------------------------- built-ins
# Built-in (packed) game songs can't be imported/ASR'd by dadtool, so the mod queues them
# to _requests.jsonl and dadtool fetches their lyrics online (LRClib), matched on duration.
LRCLIB = "https://lrclib.net"
_UA = "dadtool-lyrics/1.0 (+https://lrclib.net)"
# Generic/placeholder titles we won't fuzzy-fetch (the game's blank "No Song" asset would
# otherwise grab another track's lyrics).
_PLACEHOLDER_TITLES = {"", "no song", "untitled", "no title", "none", "no lyrics"}
_OST_CACHE = paths.CACHE_DIR / "lyrics_ost_asr.json"
DURATION_TOLERANCE_S = 8     # LRClib duration-matching tolerance (seconds)


def _load_ost_cache() -> dict:
    try:
        return json.loads(_OST_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_ost_cache(c: dict) -> None:
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _OST_CACHE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def _lrclib_get(path: str, params: dict):
    url = LRCLIB + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001  (network/parse: treat as no-result)
        return 0, None


def fetch_lrclib(artist: str, title: str, duration: int = 0) -> str | None:
    """Synced LRC text from LRClib, matched on artist/title/DURATION (so we get the right
    version). Falls back to a duration-closest search hit. Returns None if nothing synced."""
    if artist and title:
        params = {"artist_name": artist, "track_name": title}
        if duration:
            params["duration"] = int(duration)
        st, data = _lrclib_get("/api/get", params)
        if st == 200 and data and data.get("syncedLyrics"):
            return data["syncedLyrics"]
    q = {}
    if title:
        q["track_name"] = title
    if artist:
        q["artist_name"] = artist
    if not q and title:
        q = {"q": title}
    st, results = _lrclib_get("/api/search", q)
    if st == 200 and isinstance(results, list):
        best, best_score = None, None
        for c in results:
            if not c.get("syncedLyrics"):
                continue
            score = abs((c.get("duration") or 0) - (duration or 0))
            if best is None or score < best_score:
                best, best_score = c, score
        if best and (not duration or best_score <= DURATION_TOLERANCE_S):
            return best["syncedLyrics"]
    return None


def _fetch_synced_text(artist: str, title: str, duration: int = 0) -> str | None:
    """Best-available synced LRC: LRClib (duration-matched, accurate) first, then
    syncedlyrics multi-provider (Musixmatch/NetEase/Genius/Megalobiz) for broader coverage
    of niche soundtrack tracks LRClib lacks."""
    text = fetch_lrclib(artist, title, duration)
    if text:
        return text
    term = f"{title} {artist}".strip()
    if not term:
        return None
    try:
        import syncedlyrics
        for kw in ({"synced_only": True}, {}):
            try:
                r = syncedlyrics.search(term, **kw)
            except TypeError:
                continue
            except Exception:  # noqa: BLE001
                r = None
            if r and re.search(r"\[\d+:\d+", r):
                return r
    except Exception:  # noqa: BLE001
        pass
    return None


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _index_soundtrack(dirs) -> dict:
    """Map normalized-title -> audio path over the configured soundtrack dir(s). Prefers MP3
    over WAV (same audio, far smaller to decode). Strips a leading track number from names."""
    idx: dict = {}
    for d in dirs or []:
        base = Path(d)
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.suffix.lower() not in (".mp3", ".wav", ".flac", ".ogg", ".m4a"):
                continue
            stem = re.sub(r"^\s*\d+[_\-.\s]*", "", p.stem)   # drop a leading "05_" track number
            k = _normalize(stem)
            if not k:
                continue
            cur = idx.get(k)
            if cur is None or (p.suffix.lower() == ".mp3" and cur.suffix.lower() != ".mp3"):
                idx[k] = p
    return idx


def _find_soundtrack(title: str, dirs) -> Path | None:
    """Exact normalized-title match in the soundtrack (e.g. 'Hyper Sunrise' -> 05_HyperSunrise.mp3),
    with a fallback that strips a parenthetical suffix ('Echolokators (Radio Edit)' -> 'Echolokators').
    Still exact on the cleaned title -- avoids 'Mission' grabbing 'Mission_BOSS_RMX'."""
    idx = _index_soundtrack(dirs)
    k = _normalize(title)
    if k and k in idx:
        return idx[k]
    k2 = _normalize(re.sub(r"[\(\[].*?[\)\]]", "", title))   # drop "(Radio Edit)" etc.
    if k2 and k2 != k and k2 in idx:
        return idx[k2]
    return None


def _read_queue(out: Path) -> list[dict]:
    """Parse Marquee's lyrics manifest -- the full per-load `_catalog.jsonl` (every song,
    rewritten each game load), falling back to the legacy per-session `_requests.jsonl`.
    Dedup by key, preferring the entry with the richest metadata (non-empty artist, then more
    fields). process_queue filters to `not isImported` and skips keys that already have a
    `.lrc`/`.miss`, so handing it the whole catalog just lets that skip find the gaps."""
    reqfile = out / "_catalog.jsonl"           # Marquee f3d6b76+: full catalog manifest
    if not reqfile.exists():
        reqfile = out / "_requests.jsonl"      # fallback: legacy per-session queue
    if not reqfile.exists():
        return []
    best: dict = {}
    for line in reqfile.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        k = str(obj.get("key") or "")
        if not k:
            continue
        score = (1 if (obj.get("artist") or "").strip() else 0, len(obj))
        if k not in best or score > best[k][0]:
            best[k] = (score, obj)
    return [v[1] for v in best.values()]


def _find_fmod_bank(key: str) -> Path | None:
    """Find the .streams.bank file for a built-in song key (e.g. PS_ED_Versus_145)."""
    if not key.startswith("PS_"):
        return None
    bank_name = "MX_" + key[3:]
    cfg = _cfg()
    game_install_dir = cfg.get("game_install_dir")
    if not game_install_dir:
        return None
    bank_path = Path(game_install_dir) / "Pagoda" / "Content" / "FMOD" / "Banks" / "Desktop" / f"{bank_name}.streams.bank"
    if bank_path.exists():
        return bank_path
    return None


def _unpack_fmod_bank(bank_path: Path, temp_dir: Path, file_prefix: str) -> Path:
    """Extract the primary audio stream from an FMOD bank to a temp file in temp_dir.
    Monkeypatches fsb5 dynamically to resolve libvorbis and libogg dependencies
    using the game's built-in DLLs.
    """
    import os
    import sys
    import ctypes

    # Preload DLLs from game's Engine directory
    cfg = _cfg()
    game_install_dir = cfg.get("game_install_dir")
    if game_install_dir:
        game_dir = Path(game_install_dir)
        vorbis_dll = game_dir / "Engine" / "Binaries" / "ThirdParty" / "Vorbis" / "Win64" / "VS2015" / "libvorbis_64.dll"
        ogg_dll = game_dir / "Engine" / "Binaries" / "ThirdParty" / "Ogg" / "Win64" / "VS2015" / "libogg_64.dll"

        # Add DLL directories for Windows DLL resolution
        if sys.platform == 'win32' and hasattr(os, 'add_dll_directory'):
            if vorbis_dll.exists():
                os.add_dll_directory(str(vorbis_dll.parent))
            if ogg_dll.exists():
                os.add_dll_directory(str(ogg_dll.parent))

        # Preload Ogg DLL so Vorbis can resolve its dependency
        loaded_ogg = None
        if ogg_dll.exists():
            try:
                loaded_ogg = ctypes.CDLL(str(ogg_dll))
            except Exception:
                pass

        # Patch fsb5.utils.load_lib
        import fsb5.utils
        original_load_lib = fsb5.utils.load_lib

        def patched_load_lib(*names):
            for name in names:
                if name == 'vorbis' and vorbis_dll.exists():
                    try:
                        return ctypes.CDLL(str(vorbis_dll))
                    except Exception:
                        pass
                elif name == 'ogg' and ogg_dll.exists():
                    if loaded_ogg:
                        return loaded_ogg
                    try:
                        return ctypes.CDLL(str(ogg_dll))
                    except Exception:
                        pass
            return original_load_lib(*names)

        fsb5.utils.load_lib = patched_load_lib

    # Import fsb5 and extract
    import fsb5

    with open(bank_path, "rb") as f:
        data = f.read()

    fsb_offset = data.find(b"FSB5")
    if fsb_offset == -1:
        raise ValueError("No FSB5 magic header found in bank file")

    fsb_file = fsb5.FSB5(data[fsb_offset:])
    if not fsb_file.samples:
        raise ValueError("No audio samples found in FMOD bank")

    sample = fsb_file.samples[0]
    audio_data = fsb_file.rebuild_sample(sample)

    ext = fsb_file.get_sample_extension()
    if not ext.startswith("."):
        ext = "." + ext

    out_path = temp_dir / f"{file_prefix}{ext}"
    out_path.write_bytes(audio_data)
    return out_path


def process_queue(out_dir=None, *, include_imported: bool = False, force: bool = False,
                  dry_run: bool = False, allow_running: bool = False, limit: int = 0,
                  transliterate: bool = False) -> dict:
    """Process the Marquee catalog (_catalog.jsonl, falling back to legacy _requests.jsonl):
    fetch LRClib lyrics for BUILT-IN songs (which dadtool can't import). Writes <key>.lrc
    (synced, RAW timing -- built-ins have no known trim, so the player's F9/F10 fine-tunes)
    or <key>.miss. Imported songs are skipped (the importer produces those) unless
    include_imported."""
    out = Path(out_dir) if out_dir else cache_dir()
    if not mod_installed(out):
        raise RuntimeError(f"lyrics target not found ({out}); the Marquee mod isn't installed")
    model = _cfg().get("lyrics_model") or "large-v3"
    st_dirs = _cfg().get("soundtrack_dirs") or []
    ost_cache = _load_ost_cache()   # {ost_path: worker_data} -> ASR each OST file once, reuse for variants
    eff_transliterate = transliterate or _cfg().get("transliterate_lyrics", False)
    entries = [e for e in _read_queue(out) if include_imported or not e.get("isImported")]
    todo = [e for e in entries if force or not (
        (out / f"{e.get('key')}.lrc").exists() or (out / f"{e.get('key')}.miss").exists())]
    if limit:
        todo = todo[:limit]

    def _label(e):
        return f"{(e.get('artist') or '?')} - {e.get('title') or e.get('songName') or '?'}"

    if dry_run:
        rows = []
        for e in todo:
            key = str(e.get("key"))
            audio = _find_soundtrack(e.get("title") or e.get("songName") or "", st_dirs)
            if audio:
                label = f"OST:{audio.name}"
            else:
                bank_path = _find_fmod_bank(key)
                label = f"FMOD:{bank_path.name}" if bank_path else "online-or-miss"
            rows.append((key, _label(e), label))
        return {"requests": len(entries), "todo": len(todo), "ok": 0, "miss": 0, "songs": rows}
    if not allow_running and gamestate.is_game_running():
        raise RuntimeError("game is running; refusing to write")

    ok = miss = 0
    songs = []
    for e in todo:
        key = str(e.get("key"))
        artist = (e.get("artist") or "").strip()
        title = (e.get("title") or e.get("songName") or "").strip()
        dur = e.get("durationSec") or e.get("duration") or 0
        if title.lower() in _PLACEHOLDER_TITLES:
            _write_no_bom(out / f"{key}.miss", "placeholder/untitled track; no lyrics fetched\n")
            miss += 1
            songs.append((key, _label(e), "SKIP"))
            continue
        lrc_p, miss_p = out / f"{key}.lrc", out / f"{key}.miss"
        synced = _fetch_synced_text(artist, title, dur)
        if synced:
            text = synced.lstrip("﻿")
            if not text.endswith("\n"):
                text += "\n"
            tags = "".join(f"[{k}:{v}]\n" for k, v in (("ti", title), ("ar", artist)) if v)
            lrc_text = f"{tags}[by:dadtool (LRClib, built-in)]\n{text}"
            if eff_transliterate:
                lrc_text = transliterate_lrc(lrc_text)
            _write_no_bom(lrc_p, lrc_text)
            if miss_p.exists():
                miss_p.unlink()
            ok += 1
            songs.append((key, _label(e), "OK"))
            continue
        # no online lyrics -> ASR the soundtrack file if we have the built-in audio
        audio = _find_soundtrack(title, st_dirs)
        is_temp_audio = False
        bank_path = None
        if not audio:
            bank_path = _find_fmod_bank(key)
            if bank_path:
                ck = str(bank_path)
                if ck in ost_cache:
                    # Already cached from previous extraction
                    audio = bank_path
                else:
                    try:
                        temp_dir = paths.CACHE_DIR
                        temp_dir.mkdir(parents=True, exist_ok=True)
                        audio = _unpack_fmod_bank(bank_path, temp_dir, f"temp_{key}")
                        is_temp_audio = True
                    except Exception as ex:
                        print(f"Warning: Failed to extract FMOD bank for {key}: {ex}")
                        audio = None

        if audio:
            ck = str(bank_path) if bank_path else str(audio)
            data = None
            try:
                if ck in ost_cache:
                    data = ost_cache[ck]                       # reuse: this file already ASR'd
                else:
                    try:
                        data = _run_worker(audio, title, artist, "off", model)  # reference off -> pure ASR
                    except Exception:  # noqa: BLE001
                        data = None
                    if data:
                        ost_cache[ck] = data
                        _save_ost_cache(ost_cache)             # persist so later chunks skip re-ASR
            finally:
                if is_temp_audio and audio and audio.exists():
                    try:
                        audio.unlink()
                    except Exception as ex:
                        print(f"Warning: Failed to delete temporary audio file {audio}: {ex}")

            if data and not data.get("instrumental") and data.get("lines"):
                source_lbl = "built-in FMOD" if bank_path else "built-in OST"
                if eff_transliterate:
                    for l in data["lines"]:
                        l["text"] = transliterate_line(l["text"])
                    if data.get("transcript"):
                        data["transcript"] = "\n".join(transliterate_line(ln) for ln in data["transcript"].splitlines())
                _write_no_bom(lrc_p, _format_lrc(title, artist, data["lines"],
                                                 f"dadtool ASR ({source_lbl}) - DRAFT"))
                _write_no_bom(out / f"{key}.txt", (data.get("transcript") or "") + "\n")
                if miss_p.exists():
                    miss_p.unlink()
                ok += 1
                d2 = data.get("duration") or 0
                # flag if the OST cut's length differs from the in-game cut -> timing may be off
                songs.append((key, _label(e), "ASR?" if (dur and abs(d2 - dur) > 5) else "ASR"))
                continue
        _write_no_bom(miss_p, "no online lyrics; not in soundtrack/FMOD (or instrumental)\n")
        miss += 1
        songs.append((key, _label(e), "MISS"))
    return {"requests": len(entries), "todo": len(todo), "ok": ok, "miss": miss, "songs": songs}


# _normalize is defined above (near _index_soundtrack); used by both _index_soundtrack
# and remap for consistent title/artist normalization.


def _read_lrc_tags(path) -> tuple:
    """First [ti:]/[ar:] tags from an .lrc (used to match an orphan to a queued request)."""
    ti = ar = None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:15]:
            if ti is None:
                m = re.match(r"\s*\[ti:(.*)\]\s*$", line, re.I)
                if m:
                    ti = m.group(1).strip()
            if ar is None:
                m = re.match(r"\s*\[ar:(.*)\]\s*$", line, re.I)
                if m:
                    ar = m.group(1).strip()
            if ti is not None and ar is not None:
                break
    except Exception:  # noqa: BLE001
        pass
    return ti, ar


def remap(out_dir=None, *, dry_run: bool = False, allow_running: bool = False) -> dict:
    """Preserve lyrics across a built-in KEY change. For each newly-queued request whose
    <key>.lrc doesn't exist, find an orphaned <oldkey>.lrc whose [ti:]/[ar:] match the
    request and rename <oldkey>.{lrc,txt,words.json,offset} -> <newkey>.* -- keeping the
    player's F9/F10 .offset nudge + any proofing instead of re-fetching. Requests with no
    matching orphan are left for `--queue`. Imported-song lyrics (stable uniqueId keys) are
    never used as rename sources."""
    out = Path(out_dir) if out_dir else cache_dir()
    if not mod_installed(out):
        raise RuntimeError(f"lyrics target not found ({out}); the Marquee mod isn't installed")
    if not dry_run and not allow_running and gamestate.is_game_running():
        raise RuntimeError("game is running; refusing to write")

    imported_keys = set()                      # never remap a valid imported song's lyrics away
    base = paths.imported_songs_dir()
    if base.exists():
        for d in base.iterdir():
            mj = d / "Meta.json"
            if mj.exists():
                try:
                    imported_keys.add(song_key(meta.read_meta(mj)[0]))
                except Exception:  # noqa: BLE001
                    pass

    orphans = {}                               # oldkey -> (norm_title, norm_artist)
    for p in out.glob("*.lrc"):
        if p.stem in imported_keys:
            continue
        ti, ar = _read_lrc_tags(p)
        if ti:
            orphans[p.stem] = (_normalize(ti), _normalize(ar))

    remapped, unmatched, used = [], [], set()
    for e in _read_queue(out):
        newkey = str(e.get("key") or "")
        if not newkey or (out / f"{newkey}.lrc").exists() or (out / f"{newkey}.miss").exists():
            continue                           # already resolved (has lyrics or known-miss)
        title = _normalize(e.get("title") or e.get("songName") or "")
        artist = _normalize(e.get("artist") or "")
        if not title:
            continue
        match = next((k for k, (ti, ar) in orphans.items()
                      if k != newkey and k not in used
                      and ti == title and (not artist or not ar or ar == artist)), None)
        if not match:
            unmatched.append((newkey, e.get("title")))
            continue
        moved = []
        for ext in (".lrc", ".txt", ".words.json", ".offset"):
            src, dst = out / f"{match}{ext}", out / f"{newkey}{ext}"
            if src.exists():
                if not dry_run:
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)
                moved.append(ext)
        if not dry_run:
            miss = out / f"{newkey}.miss"
            if miss.exists():
                miss.unlink()
        used.add(match)
        remapped.append((match, newkey, e.get("title"), ",".join(moved)))
    return {"remapped": remapped, "unmatched": unmatched}


def _lrcgen_python() -> str | None:
    p = _cfg().get("lrcgen_python")
    return p if p and Path(p).exists() else None


def song_key(m: dict) -> str:
    """The stable cache key, matching the mod's lyrics_resolver.lua cleanKey(): imported
    songs key on uniqueId; built-ins on the asset short-name. We only ever import."""
    uid = m.get("uniqueId")
    if uid in (None, 0, "0", ""):
        return (m.get("uEAssetName") or "unknown").rsplit(".", 1)[-1] or "unknown"
    return str(uid)


def _artist_title(m: dict) -> tuple[str, str]:
    """Split the song into (artist, title). songName is '<artist> - <title>' here, with a
    blank performedBy; honor performedBy[0] if a song ever has it."""
    name = (m.get("songName") or "").strip()
    perf = m.get("performedBy") or []
    if isinstance(perf, list) and perf:
        artist = str(perf[0]).strip()
        title = name.split(" - ", 1)[-1] if " - " in name else name
    elif " - " in name:
        artist, title = (p.strip() for p in name.split(" - ", 1))
    else:
        artist, title = "", name
    return artist, title


def _ts(t: float) -> str:
    if not t or t < 0:
        t = 0.0
    m = int(t // 60)
    return f"{m:02d}:{t - m * 60:05.2f}"


def _run_worker(audio, title, artist, reference, model, timeout: int = 1800) -> dict:
    py = _lrcgen_python()
    if not py or not WORKER.exists():
        raise RuntimeError("lyrics ASR unavailable: set 'lrcgen_python' in dad_config.json "
                           "to the lyrics venv python (and ensure scripts/lrcgen_worker.py exists)")
    cmd = [py, str(WORKER), "--audio", str(audio), "--title", title, "--artist", artist,
           "--reference", reference, "--model", model, "--models-dir", str(MODELS_DIR)]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"lrcgen worker failed: {(out.stderr or '').strip()[-400:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])  # last line == the JSON


def _backup_existing(path: Path) -> None:
    if path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (BACKUP_DIR / f"{path.name}.{ts}.bak").write_bytes(path.read_bytes())


_KAKASI = None


def _get_kakasi():
    global _KAKASI
    if _KAKASI is None:
        import pykakasi
        _KAKASI = pykakasi.kakasi()
    return _KAKASI


def is_japanese(text: str) -> bool:
    # Check for Hiragana, Katakana, or CJK Unified Ideographs (Kanji)
    return any(re.search(r'[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf]', char) for char in text)


def transliterate_line(line: str) -> str:
    """Convert Japanese Kanji/Kana to Romaji and other non-ASCII languages to Latin ASCII."""
    import anyascii

    k = _get_kakasi()
    tokens = k.convert(line)
    result_parts = []
    current_non_ja = ""

    for item in tokens:
        orig = item['orig']
        hepburn = item['hepburn']

        if is_japanese(orig):
            if current_non_ja:
                result_parts.append(anyascii.anyascii(current_non_ja))
                current_non_ja = ""
            result_parts.append(hepburn)
        else:
            current_non_ja += orig

    if current_non_ja:
        result_parts.append(anyascii.anyascii(current_non_ja))

    raw_joined = " ".join(result_parts)
    cleaned = re.sub(r' +', ' ', raw_joined)
    return cleaned


def transliterate_lrc(text: str) -> str:
    """Apply transliteration to an entire LRC block, preserving timestamps and tag formats."""
    out_lines = []
    for line in text.splitlines():
        # Match metadata tag: [ti:Title]
        meta_m = re.match(r'^\s*\[([a-zA-Z]+):(.*)\]\s*$', line)
        if meta_m:
            tag, val = meta_m.group(1), meta_m.group(2)
            trans_val = transliterate_line(val)
            out_lines.append(f"[{tag}:{trans_val}]")
            continue

        # Match timed lyric: [01:23.45] lyric text
        time_m = re.match(r'^\s*((?:\[\d{1,2}:\d{2}(?:\.\d{1,3})?\])+)(.*)$', line)
        if time_m:
            stamps = time_m.group(1)
            lyric_text = time_m.group(2)
            trans_lyric = transliterate_line(lyric_text)
            out_lines.append(f"{stamps}{trans_lyric}")
            continue

        # Fallback
        out_lines.append(transliterate_line(line))

    return "\n".join(out_lines) + "\n"


def _write_no_bom(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")  # utf-8 (no BOM), LF endings


def _format_lrc(title: str, artist: str, lines: list[dict], by: str = BY_TAG) -> str:
    head = []
    if title:
        head.append(f"[ti:{title}]")
    if artist:
        head.append(f"[ar:{artist}]")
    head += [f"[by:{by}]", ""]
    body = [f"[{_ts(l['t'])}]{l['text']}" for l in lines]
    return "\n".join(head + body) + "\n"


def generate(folder: str, *, reference: str = "auto", timing: str | None = None,
             model: str | None = None, out_dir=None, allow_running: bool = False,
             transliterate: bool = False) -> dict:
    """Generate (or refresh) the .lrc for an imported song folder. Writes into the mod
    cache (or out_dir). Refuses while the game is running unless allow_running."""
    if not allow_running:
        procs = gamestate.is_game_running()
        if procs:
            raise RuntimeError(f"game is running ({', '.join(procs)}); refusing to write")

    out = Path(out_dir) if out_dir else cache_dir()
    if not mod_installed(out):
        raise RuntimeError(f"lyrics target not found ({out}); the Marquee mod isn't installed "
                           "(pass out_dir to override)")

    base = paths.imported_songs_dir() / folder
    m, _ = meta.read_meta(base / "Meta.json")
    audio = base / "Audio.ogg"
    if not audio.exists():
        raise FileNotFoundError(audio)

    key = song_key(m)
    artist, title = _artist_title(m)
    model = model or _cfg().get("lyrics_model") or "large-v3"
    timing = timing or _cfg().get("lyrics_timing_model") or "auto"
    start_off = float(m.get("startSongOffset") or 0.0)

    data = _run_worker(audio, title, artist, reference, model)
    eff_transliterate = transliterate or _cfg().get("transliterate_lyrics", False)

    out.mkdir(parents=True, exist_ok=True)
    lrc_p = out / f"{key}.lrc"
    txt_p = out / f"{key}.txt"
    words_p = out / f"{key}.words.json"
    miss_p = out / f"{key}.miss"

    if data.get("instrumental"):
        _backup_existing(lrc_p)
        if lrc_p.exists():
            lrc_p.unlink()
        _write_no_bom(miss_p, "instrumental: no vocal lyrics detected by ASR\n")
        return {"key": key, "instrumental": True, "out": str(miss_p), "title": title}

    src = data.get("source", "asr")
    # Timing by source: ASR runs on the trimmed shipped audio (already 0-based -> no shift);
    # online synced lyrics are timed to the ORIGINAL release (-> subtract the trim). 'auto'
    # picks per source; an explicit timing= overrides it.
    eff_timing = (("subtract-start-offset" if src == "online" else "file")
                  if timing == "auto" else timing)
    lines = data["lines"]
    words = data.get("words") or []
    if eff_transliterate:
        for l in lines:
            l["text"] = transliterate_line(l["text"])
        for w in words:
            w["word"] = transliterate_line(w["word"])
        if data.get("transcript"):
            data["transcript"] = "\n".join(transliterate_line(ln) for ln in data["transcript"].splitlines())

    if eff_timing == "subtract-start-offset":
        for l in lines:
            l["t"] = max(0.0, round(l["t"] - start_off, 2))
        lines = [l for l in lines if l["t"] >= 0.0]
        for w in words:
            w["start"] = max(0.0, round(w["start"] - start_off, 3))
            w["end"] = max(0.0, round(w["end"] - start_off, 3))

    by = "dadtool (online synced LRC, trim-corrected)" if src == "online" else BY_TAG
    _backup_existing(lrc_p)
    _write_no_bom(lrc_p, _format_lrc(title, artist, lines, by))
    _write_no_bom(txt_p, (data.get("transcript") or "") + "\n")
    if words:
        _write_no_bom(words_p, json.dumps(words, ensure_ascii=False, indent=2))
    # Clear a stale .miss so the new lyrics aren't hidden. Do NOT touch <key>.offset: per
    # Marquee's spec that's the player's live F9/F10/F11 fine-tune, not ours to remove.
    if miss_p.exists():
        _backup_existing(miss_p)
        miss_p.unlink()

    return {"key": key, "instrumental": False, "out": str(lrc_p), "title": title, "artist": artist,
            "source": src, "lines": len(lines), "language": data.get("language"),
            "corrected": data.get("corrected"), "reconciled": data.get("reconciled"),
            "dropped": data.get("dropped"), "timing": eff_timing, "start_off": start_off}
