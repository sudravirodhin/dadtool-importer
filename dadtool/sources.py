"""Source-audio organization: keep originals in the workspace, not scattered in
Downloads. Two entry points:

- relocate_existing(): one-time — move each imported song's source (from its
  Meta.json ``originalAudioFilePath``) into audio/processed/.
- ingest_pending(): going-forward — scan audio/pending/, match files to imported
  songs by the MD5 the game records, and move matched sources to audio/processed/.
  Unmatched files stay in pending/ (= not imported into the game yet).

Only filesystem moves happen here (never the game's Saved dir), so no backup or
game-running gate is needed. The stale ``originalAudioFilePath`` is healed on the
next ``batch`` write.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from . import meta, paths


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    i = 1
    while (dest.parent / f"{dest.stem} ({i}){dest.suffix}").exists():
        i += 1
    return dest.parent / f"{dest.stem} ({i}){dest.suffix}"


def move_into(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest_dir / src.name)
    shutil.move(str(src), str(dest))
    return dest


def relocate_existing(dry_run: bool = False) -> dict:
    base = paths.imported_songs_dir()
    moved, missing, already = [], [], []
    pdir = paths.PROCESSED_DIR.resolve()
    for d in sorted(base.iterdir()):
        mj = d / "Meta.json"
        if not d.is_dir() or not mj.exists():
            continue
        data, _ = meta.read_meta(mj)
        oap = (data.get("originalAudioFilePath") or "").strip()
        if not oap:
            continue
        srcp = Path(oap)
        if srcp.exists() and srcp.parent.resolve() != pdir:
            dest = str(paths.PROCESSED_DIR / srcp.name) if dry_run else str(move_into(srcp, paths.PROCESSED_DIR))
            moved.append((oap, dest))
        elif (paths.PROCESSED_DIR / srcp.name).exists():
            already.append(srcp.name)
        else:
            missing.append(oap)
    return {"moved": moved, "missing": missing, "already": already}


def ingest_pending(dry_run: bool = False) -> dict:
    base = paths.imported_songs_dir()
    want: dict[str, str] = {}
    for d in sorted(base.iterdir()):
        mj = d / "Meta.json"
        if d.is_dir() and mj.exists():
            data, _ = meta.read_meta(mj)
            h = data.get("originalAudioFileHash")
            if h:
                want[h] = d.name
    paths.PENDING_DIR.mkdir(parents=True, exist_ok=True)
    moved, unmatched = [], []
    for f in sorted(paths.PENDING_DIR.iterdir()):
        if not f.is_file():
            continue
        song = want.get(_md5(f))
        if song:
            dest = f.name if dry_run else str(move_into(f, paths.PROCESSED_DIR))
            moved.append((f.name, song, dest))
        else:
            unmatched.append(f.name)
    return {"moved": moved, "unmatched": unmatched}
