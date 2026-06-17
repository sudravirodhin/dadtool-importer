"""Analysis cache keyed by the source-audio hash (QoL #2).

Re-runs become instant, and the data survives the game wiping or changing its
save during an early-access patch (pairs with `restore`). Each entry records the
game build id and FORMAT.md hash it was produced under, so a version/format
change invalidates stale entries.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from . import paths
from .snapshot import sha256_file

CACHE_FILE = paths.CACHE_DIR / "analysis_cache.json"


def format_hash() -> str | None:
    if paths.FORMAT_MD.exists():
        return hashlib.sha256(paths.FORMAT_MD.read_bytes()).hexdigest()[:16]
    return None


def load() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"schema": 1, "entries": {}}


def save(cache: dict) -> None:
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def key_for(meta_dict: dict, ogg_path) -> str:
    """Prefer the stable source-file MD5 the game records; fall back to the ogg."""
    h = meta_dict.get("originalAudioFileHash")
    if h:
        return f"src:{h}"
    return f"ogg:{sha256_file(Path(ogg_path))[:32]}"


def get(cache: dict, key: str):
    return cache.get("entries", {}).get(key)


def fresh(entry: dict | None, build_id, fmt_hash, analyzer_version=None) -> bool:
    return (bool(entry) and entry.get("game_build_id") == build_id
            and entry.get("format_hash") == fmt_hash
            and entry.get("analyzer_version") == analyzer_version)


def put(cache: dict, key: str, song: str, analysis: dict, build_id, fmt_hash,
        analyzer_version=None) -> None:
    cache.setdefault("entries", {})[key] = {
        "song": song,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "game_build_id": build_id,
        "format_hash": fmt_hash,
        "analyzer_version": analyzer_version,
        "analysis": analysis,
    }
