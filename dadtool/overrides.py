"""Manual per-song override file (QoL #4).

Pin values the detector gets wrong once, and every future run respects them.
Keys = exact ImportedSongs folder name. Recognized fields: tempo, beatOffset,
startSongOffset, customTempoSections, skip. Any other key (e.g. a note) is
ignored, so you can annotate freely.
"""
from __future__ import annotations

import json

from . import paths

OVERRIDES_FILE = paths.REPO_ROOT / "overrides.json"
ALLOWED = {"tempo", "beatOffset", "startSongOffset", "endSongOffset", "customTempoSections", "skip"}


def load() -> dict:
    if OVERRIDES_FILE.exists():
        try:
            data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def for_song(song: str, data: dict | None = None) -> dict:
    data = data if data is not None else load()
    ov = data.get(song) or {}
    return {k: v for k, v in ov.items() if k in ALLOWED}
