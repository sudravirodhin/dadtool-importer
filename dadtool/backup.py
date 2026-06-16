"""Full, timestamped backups of the game's Saved dir.

Mandatory before ANY write to the game directory. Backups go into the repo's
``backups/`` folder (git-ignored) and are never auto-pruned — especially not
across a game version boundary.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from . import paths


def _count_tree(root: Path) -> tuple[int, int]:
    files = [p for p in root.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def backup_saved(reason: str = "", timestamp: str | None = None) -> Path:
    """Copy the entire Saved tree into backups/saved_<ts>/ and verify the copy.

    Returns the backup directory. Raises if the file count/size don't match.
    """
    src = paths.saved_dir()
    if not src.exists():
        raise FileNotFoundError(f"Saved dir not found: {src}")
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = paths.BACKUPS_DIR / f"saved_{ts}"
    if dest.exists():
        raise FileExistsError(f"Backup dest already exists: {dest}")
    paths.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    shutil.copytree(src, dest)

    sc, ss = _count_tree(src)
    dc, ds = _count_tree(dest)
    if (sc, ss) != (dc, ds):
        raise RuntimeError(
            f"Backup verification FAILED: src=({sc} files,{ss} B) "
            f"dest=({dc} files,{ds} B)"
        )
    (dest / "_backup_info.txt").write_text(
        f"source={src}\ncreated={ts}\nreason={reason}\nfiles={dc}\nbytes={ds}\n",
        encoding="utf-8",
    )
    return dest
