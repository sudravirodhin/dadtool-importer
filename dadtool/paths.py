r"""Path resolution for the Dead as Disco import tool.

The game directory is an EXTERNAL, CONFIGURABLE target and must never be
hardcoded in logic outside this module. Override order:

  1. ``DAD_SAVED_DIR`` environment variable
  2. ``dad_config.json`` in the repo root  ({"saved_dir": "..."})
  3. Default: %LOCALAPPDATA%\Pagoda\Saved
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "dad_config.json"

SNAPSHOTS_DIR = REPO_ROOT / "snapshots"
BACKUPS_DIR = REPO_ROOT / "backups"
CACHE_DIR = REPO_ROOT / "cache"
FORMAT_MD = REPO_ROOT / "FORMAT.md"

# Source-audio staging in the workspace (not the game dir)
AUDIO_DIR = REPO_ROOT / "audio"
PENDING_DIR = AUDIO_DIR / "pending"      # drop new source audio here
PROCESSED_DIR = AUDIO_DIR / "processed"  # sources that have been imported


def _default_saved_dir() -> Path:
    lad = os.environ.get("LOCALAPPDATA", "")
    return Path(lad) / "Pagoda" / "Saved"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def saved_dir() -> Path:
    """Resolve the game's Saved directory (the external target root)."""
    env = os.environ.get("DAD_SAVED_DIR")
    if env:
        return Path(env)
    cfg = load_config()
    if cfg.get("saved_dir"):
        return Path(cfg["saved_dir"])
    return _default_saved_dir()


def imported_songs_dir() -> Path:
    return saved_dir() / "ImportedSongs"


def savegames_dir() -> Path:
    return saved_dir() / "SaveGames"
