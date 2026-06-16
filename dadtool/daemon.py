"""Background watcher: auto external-import songs dropped into audio/pending/.

Drop an audio file in pending/ and the watcher transcodes -> analyzes -> writes the
ImportedSongs entry (no in-game importer), then files the source into processed/.
New songs appear in-game, pre-synced, on the next launch.

Notes:
  - External import only CREATES a new folder (never edits existing songs/saves), so
    it's safe to run while the game is open; the song shows up on the next launch.
  - One backup is taken when the watcher first processes something (not per song,
    since imports are additive).
  - Files that fail (or whose song folder already exists) are moved to pending/_failed/.
  - Exact-duplicate drops (same audio already imported) are moved to pending/_duplicates/.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import backup, importer, paths, sources

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".opus", ".aac"}


def _process_once(sizes: dict, state: dict, stable_only: bool, log, limit: int = 0) -> int:
    pending = paths.PENDING_DIR
    processed = paths.PROCESSED_DIR
    failed = pending / "_failed"
    n = 0
    for f in sorted(pending.iterdir()):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        sz = f.stat().st_size
        if stable_only and sizes.get(f.name) != sz:
            sizes[f.name] = sz  # size still changing => still being copied; wait a poll
            continue
        sizes.pop(f.name, None)
        try:
            if not state["backed_up"]:
                log("backup: " + str(backup.backup_saved(reason="daemon first import")))
                state["backed_up"] = True
            # File the source into processed/ FIRST, then import from there, so the
            # originalAudioFilePath we record is the final location (not the now-gone
            # pending path - which made the game warn "original source no longer exists").
            dest = sources.move_into(f, processed)
            try:
                r = importer.external_import(str(dest), allow_running=True, do_backup=False)
            except importer.DuplicateSongError as de:
                sources.move_into(dest, pending / "_duplicates")
                log(f"duplicate of {de.existing_folder!r}: {f.name}  -> _duplicates/")
                continue
            except Exception:
                sources.move_into(dest, failed)  # quarantine the moved source on failure
                raise
            n += 1
            flags = ("  | " + "; ".join(r["flags"])) if r["flags"] else ""
            log(f"imported: {r['songName']!r} - {r['artist']!r}  {r['tempo']} BPM  "
                f"{r['consistency']} conf {r['confidence']} sections {r['sections']}{flags}")
            if limit and n >= limit:
                break
        except FileExistsError:
            log(f"skip (song folder already exists): {f.name}  -> _failed/")
        except Exception as e:  # noqa: BLE001
            log(f"ERROR {f.name}: {e}  -> _failed/")
    return n


def watch(interval: float = 5.0, do_initial_backup: bool = True, once: bool = False,
          limit: int = 0, log=print) -> None:
    paths.PENDING_DIR.mkdir(parents=True, exist_ok=True)
    paths.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    state = {"backed_up": not do_initial_backup}
    sizes: dict = {}
    if once:
        count = _process_once(sizes, state, stable_only=False, log=log, limit=limit)
        log(f"done: {count} imported")
        return
    log(f"watching {paths.PENDING_DIR} every {interval}s - drop audio files to auto-import (Ctrl+C to stop)")
    while True:
        _process_once(sizes, state, stable_only=True, log=log)
        time.sleep(interval)
