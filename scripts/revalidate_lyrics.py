import os
import sys
import json
import re
import urllib.request
import urllib.parse
import subprocess
from pathlib import Path

# Enforce UTF-8 output on Windows
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Paths
REPO_ROOT = Path(__file__).resolve().parent.parent
DAD_CONFIG_FILE = REPO_ROOT / "dad_config.json"
SCRIPTS_DIR = REPO_ROOT / "scripts"

LRCLIB = "https://lrclib.net"
_UA = "dadtool-lyrics/1.0 (+https://lrclib.net)"
DURATION_TOLERANCE_S = 8

def log(*args):
    print(*args, file=sys.stderr, flush=True)

def load_config() -> dict:
    if DAD_CONFIG_FILE.exists():
        try:
            return json.loads(DAD_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"Error reading config: {e}")
    return {}

def read_meta_json(path) -> dict:
    raw = Path(path).read_bytes()
    if raw[:2] == b"\xff\xfe" or raw[:2] == b"\xfe\xff":
        return json.loads(raw.decode("utf-16"))
    if raw[:3] == b"\xef\xbb\xbf":
        return json.loads(raw.decode("utf-8-sig"))
    return json.loads(raw.decode("utf-8"))

def cache_dir(cfg) -> Path:
    explicit = cfg.get("lyrics_cache_dir")
    if explicit:
        return Path(explicit)
    install = cfg.get("game_install_dir")
    if install:
        return (Path(install) / "Pagoda" / "Binaries" / "Win64"
                / "ue4ss" / "Mods" / "Marquee" / "Scripts" / "data" / "lyrics")
    return Path("D:/SteamLibrary/steamapps/common/Dead as Disco/Pagoda/Binaries/Win64/ue4ss/Mods/Marquee/Scripts/data/lyrics")

def imported_songs_dir(cfg) -> Path:
    sdir = cfg.get("saved_dir")
    if sdir:
        return Path(sdir) / "ImportedSongs"
    return Path(os.path.expandvars("%LOCALAPPDATA%")) / "Pagoda" / "Saved" / "ImportedSongs"

def get_audio_duration(path: Path) -> float:
    # Use ffmpeg from imageio_ffmpeg site-packages
    ffmpeg = REPO_ROOT / ".venv" / "Lib" / "site-packages" / "imageio_ffmpeg" / "binaries" / "ffmpeg-win-x86_64-v7.1.exe"
    if not ffmpeg.exists():
        # Fall back to standard command
        ffmpeg = Path("ffmpeg.exe")
    cmd = [str(ffmpeg), '-i', str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, errors='ignore')
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out.stderr)
        if m:
            hours = int(m.group(1))
            minutes = int(m.group(2))
            seconds = float(m.group(3))
            return hours * 3600 + minutes * 60 + seconds
    except Exception as e:
        log(f"Error getting duration for {path.name}: {e}")
    return 0.0

def _lrclib_get(path: str, params: dict):
    url = LRCLIB + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception:
        return 0, None

def fetch_lrclib(artist: str, title: str, duration: float = 0.0) -> str | None:
    if artist and title:
        params = {"artist_name": artist, "track_name": title}
        if duration > 0:
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
        if best and (duration == 0 or best_score <= DURATION_TOLERANCE_S):
            return best["syncedLyrics"]
    return None

def parse_lrc_lines(text: str) -> list:
    lines = []
    if not text:
        return lines
    for ln in text.splitlines():
        m = re.match(r"\s*((?:\[\d{1,2}:\d{2}(?:\.\d{1,3})?\])+)(.*)", ln)
        if not m:
            continue
        txt = re.sub(r"<\d{1,2}:\d{2}(?:\.\d{1,3})?>", "", m.group(2)).strip()
        if not txt:
            continue
        for mm, ss in re.findall(r"\[(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\]", m.group(1)):
            lines.append({"t": round(int(mm) * 60 + float(ss), 2), "text": txt})
    lines.sort(key=lambda x: x["t"])
    return lines

def clean_title(title):
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title)
    t = re.sub(r"\s*(feat\.|ft\.|featuring)\s.*$", "", t, flags=re.I)
    return t.strip() or title.strip()

def read_lrc_meta(text: str) -> tuple:
    ti = ar = None
    for line in text.splitlines()[:15]:
        if ti is None:
            m = re.match(r"\s*\[ti:(.*)\]\s*$", line, re.I)
            if m:
                ti = m.group(1).strip()
        if ar is None:
            m = re.match(r"\s*\[ar:(.*)\]\s*$", line, re.I)
            if m:
                ar = m.group(1).strip()
    return ti, ar

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="overwrite mismatched files with correct lyrics")
    ap.add_argument("--limit", type=int, default=0, help="limit number of files to process")
    args = ap.parse_args()

    cfg = load_config()
    ldir = cache_dir(cfg)
    idir = imported_songs_dir(cfg)
    catalog_path = ldir / "_catalog.jsonl"

    log(f"Lyrics directory: {ldir}")
    log(f"Imported songs directory: {idir}")

    if not ldir.exists():
        log("Error: Lyrics cache directory does not exist.")
        sys.exit(1)

    # Load catalog for built-in song durations
    catalog = {}
    if catalog_path.exists():
        try:
            for line in catalog_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                k = obj.get("key")
                if k:
                    catalog[k] = obj
        except Exception as e:
            log(f"Warning: Failed to load catalog: {e}")

    # Load all imported song metadata/durations
    imported = {}
    if idir.exists():
        for d in idir.iterdir():
            if d.is_dir():
                mj = d / "Meta.json"
                audio = d / "Audio.ogg"
                if mj.exists() and audio.exists():
                    try:
                        meta_data = read_meta_json(mj)
                        uid = str(meta_data.get("uniqueId"))
                        if uid:
                            imported[uid] = {
                                "folder": d.name,
                                "path": audio,
                                "meta": meta_data
                            }
                    except Exception as e:
                        log(f"Warning: Failed to parse Meta.json in {d.name}: {e}")

    lrc_files = sorted(list(ldir.glob("*.lrc")), key=lambda p: p.name)
    if args.limit > 0:
        lrc_files = lrc_files[:args.limit]

    log(f"Found {len(lrc_files)} cached LRC files to validate.\n")

    print(f"| Key | Artist - Title | Duration (s) | Status | Details |")
    print(f"| --- | --- | --- | --- | --- |")

    mismatches = 0
    fixed_count = 0
    ok_count = 0
    no_online_count = 0

    for p in lrc_files:
        key = p.stem
        cached_text = p.read_text(encoding="utf-8", errors="ignore")
        ti, ar = read_lrc_meta(cached_text)
        
        # Determine duration
        duration = 0.0
        dur_src = ""
        
        if key in imported:
            # For imported songs, compute exact duration from Ogg file
            audio_path = imported[key]["path"]
            duration = get_audio_duration(audio_path)
            dur_src = "Audio.ogg"
        elif key in catalog:
            duration = float(catalog[key].get("durationSec") or 0.0)
            dur_src = "_catalog.jsonl"
            
        if not ti or not ar:
            # Fall back to catalog metadata
            if key in catalog:
                ti = catalog[key].get("title", ti)
                ar = catalog[key].get("artist", ar)
            elif key in imported:
                ti = imported[key]["meta"].get("songName", ti)

        if not ti:
            print(f"| {key} | Unknown Song | {duration:.1f} | SKIPPED | Missing title metadata in LRC |")
            continue

        label = f"{(ar or 'Unknown')} - {ti}"
        
        if duration <= 0.0:
            print(f"| {key} | {label} | 0.0 | SKIPPED | Unknown duration |")
            continue

        # Fetch duration-matched lyrics from LRClib
        online_text = fetch_lrclib(ar, ti, duration)
        
        if not online_text:
            # Check if this is ASR-generated
            is_asr = "ASR" in cached_text or "faster-whisper" in cached_text
            status = "ASR_DRAFT" if is_asr else "NO_ONLINE_SYNC"
            details = "No synced lyrics on LRClib matching duration"
            print(f"| {key} | {label} | {duration:.1f} | {status} | {details} |")
            no_online_count += 1
            continue

        # Parse both and compare timestamps of first 3 lines
        cached_lines = parse_lrc_lines(cached_text)
        online_lines = parse_lrc_lines(online_text)

        if not cached_lines or not online_lines:
            print(f"| {key} | {label} | {duration:.1f} | SKIPPED | Failed to parse timestamps |")
            continue

        # Compare timestamps of first few lines (up to 3)
        mismatch = False
        compare_limit = min(3, len(cached_lines), len(online_lines))
        diffs = []
        for i in range(compare_limit):
            diff = abs(cached_lines[i]["t"] - online_lines[i]["t"])
            diffs.append(diff)
            if diff > 1.0:
                mismatch = True

        if mismatch:
            mismatches += 1
            max_diff = max(diffs)
            details = f"Timestamps differ (max diff: {max_diff:.2f}s). Cached starts at {cached_lines[0]['t']:.2f}s, online at {online_lines[0]['t']:.2f}s."
            
            if args.fix:
                # Overwrite cached file
                tags = "".join(f"[{k}:{v}]\n" for k, v in (("ti", ti), ("ar", ar)) if v)
                by_tag = "dadtool (online synced LRC, trim-corrected)"
                new_content = f"{tags}[by:{by_tag}]\n{online_text.lstrip('﻿')}"
                if not new_content.endswith("\n"):
                    new_content += "\n"
                p.write_text(new_content, encoding="utf-8", newline="\n")
                fixed_count += 1
                status = "FIXED"
            else:
                status = "MISMATCH"
                
            print(f"| {key} | {label} | {duration:.1f} | **{status}** | {details} |")
        else:
            ok_count += 1
            print(f"| {key} | {label} | {duration:.1f} | OK | Timestamps align within {max(diffs):.2f}s |")

    log(f"\nRevalidation complete: {ok_count} OK, {mismatches} mismatches, {no_online_count} no online matches.")
    if args.fix:
        log(f"Fixed {fixed_count} mismatched files.")

if __name__ == "__main__":
    main()
