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

# The mod resolves this relative to the game's working dir; it's the LIVE 'Marquee' mod
# (the old 'DiscoTracker' folder is stale). Override via 'lyrics_cache_dir' in the config.
DEFAULT_CACHE = (r"D:\SteamLibrary\steamapps\common\Dead as Disco\Pagoda\Binaries"
                 r"\Win64\ue4ss\Mods\Marquee\Scripts\data\lyrics")
BY_TAG = "dadtool ASR (faster-whisper large-v3) - DRAFT, proof the .txt"


def _cfg() -> dict:
    return paths.load_config()


def cache_dir() -> Path:
    return Path(_cfg().get("lyrics_cache_dir") or DEFAULT_CACHE)


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
    .offset/.txt/.words.json) so a regenerate starts clean. KEEPS .gitkeep and _requests.jsonl
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
        if p.is_file() and p.name not in (".gitkeep", "_requests.jsonl"):
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


def _load_ost_cache() -> dict:
    try:
        return json.loads(_OST_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
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
        if best and (not duration or best_score <= 8):
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
        import re as _re

        import syncedlyrics
        for kw in ({"synced_only": True}, {}):
            try:
                r = syncedlyrics.search(term, **kw)
            except TypeError:
                continue
            except Exception:  # noqa: BLE001
                r = None
            if r and _re.search(r"\[\d+:\d+", r):
                return r
    except Exception:  # noqa: BLE001
        pass
    return None


def _norm_title(s: str) -> str:
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
            k = _norm_title(stem)
            if not k:
                continue
            cur = idx.get(k)
            if cur is None or (p.suffix.lower() == ".mp3" and cur.suffix.lower() != ".mp3"):
                idx[k] = p
    return idx


def _find_soundtrack(title: str, dirs) -> "Path | None":
    """Exact normalized-title match in the soundtrack (e.g. 'Hyper Sunrise' -> 05_HyperSunrise.mp3),
    with a fallback that strips a parenthetical suffix ('Echolokators (Radio Edit)' -> 'Echolokators').
    Still exact on the cleaned title -- avoids 'Mission' grabbing 'Mission_BOSS_RMX'."""
    idx = _index_soundtrack(dirs)
    k = _norm_title(title)
    if k and k in idx:
        return idx[k]
    k2 = _norm_title(re.sub(r"[\(\[].*?[\)\]]", "", title))   # drop "(Radio Edit)" etc.
    if k2 and k2 != k and k2 in idx:
        return idx[k2]
    return None


def _read_queue(out: Path) -> list[dict]:
    """Parse the mod's _requests.jsonl, dedup by key (the mod appends per session),
    preferring the entry with the richest metadata (a non-empty artist, then more fields)."""
    reqfile = out / "_requests.jsonl"
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


def process_queue(out_dir=None, *, include_imported: bool = False, force: bool = False,
                  dry_run: bool = False, allow_running: bool = False, limit: int = 0) -> dict:
    """Drain _requests.jsonl: fetch LRClib lyrics for BUILT-IN songs (which dadtool can't
    import). Writes <key>.lrc (synced, RAW timing -- built-ins have no known trim, so the
    player's F9/F10 fine-tunes) or <key>.miss. Imported songs are skipped (the importer
    produces those) unless include_imported."""
    out = Path(out_dir) if out_dir else cache_dir()
    if not mod_installed(out):
        raise RuntimeError(f"lyrics target not found ({out}); the Marquee mod isn't installed")
    model = _cfg().get("lyrics_model") or "large-v3"
    st_dirs = _cfg().get("soundtrack_dirs") or []
    ost_cache = _load_ost_cache()   # {ost_path: worker_data} -> ASR each OST file once, reuse for variants
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
            audio = _find_soundtrack(e.get("title") or e.get("songName") or "", st_dirs)
            rows.append((str(e.get("key")), _label(e),
                         f"OST:{audio.name}" if audio else "online-or-miss"))
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
            _write_no_bom(lrc_p, f"{tags}[by:dadtool (LRClib, built-in)]\n{text}")
            if miss_p.exists():
                miss_p.unlink()
            ok += 1
            songs.append((key, _label(e), "OK"))
            continue
        # no online lyrics -> ASR the soundtrack file if we have the built-in audio
        audio = _find_soundtrack(title, st_dirs)
        if audio:
            ck = str(audio)
            if ck in ost_cache:
                data = ost_cache[ck]                       # reuse: this OST file already ASR'd
            else:
                try:
                    data = _run_worker(audio, title, artist, "off", model)  # reference off -> pure ASR
                except Exception:  # noqa: BLE001
                    data = None
                if data:
                    ost_cache[ck] = data
                    _save_ost_cache(ost_cache)             # persist so later chunks skip re-ASR
            if data and not data.get("instrumental") and data.get("lines"):
                _write_no_bom(lrc_p, _format_lrc(title, artist, data["lines"],
                                                 "dadtool ASR (built-in OST) - DRAFT"))
                _write_no_bom(out / f"{key}.txt", (data.get("transcript") or "") + "\n")
                if miss_p.exists():
                    miss_p.unlink()
                ok += 1
                d2 = data.get("duration") or 0
                # flag if the OST cut's length differs from the in-game cut -> timing may be off
                songs.append((key, _label(e), "ASR?" if (dur and abs(d2 - dur) > 5) else "ASR"))
                continue
        _write_no_bom(miss_p, "no online lyrics; not in soundtrack (or instrumental)\n")
        miss += 1
        songs.append((key, _label(e), "MISS"))
    return {"requests": len(entries), "todo": len(todo), "ok": ok, "miss": miss, "songs": songs}


def _norm_match(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


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
            orphans[p.stem] = (_norm_match(ti), _norm_match(ar))

    remapped, unmatched, used = [], [], set()
    for e in _read_queue(out):
        newkey = str(e.get("key") or "")
        if not newkey or (out / f"{newkey}.lrc").exists() or (out / f"{newkey}.miss").exists():
            continue                           # already resolved (has lyrics or known-miss)
        title = _norm_match(e.get("title") or e.get("songName") or "")
        artist = _norm_match(e.get("artist") or "")
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
             model: str | None = None, out_dir=None, allow_running: bool = False) -> dict:
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
