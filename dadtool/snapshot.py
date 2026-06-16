"""Snapshot & diff the game's Saved tree (Phase 1, steps 2 & 4).

A snapshot is a manifest: every file under a root mapped to {size, sha256}.
Diffing a before/after snapshot pinpoints exactly which file(s) the game wrote
during an in-game action. Python (not PowerShell) is used deliberately so that
song folders containing characters like ``[ ]`` — which PowerShell treats as
path wildcards — are handled correctly.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def snapshot_tree(root: Path) -> dict:
    root = Path(root)
    files: dict[str, dict] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root)).replace("\\", "/")
            try:
                files[rel] = {"size": p.stat().st_size, "sha256": sha256_file(p)}
            except (PermissionError, OSError) as e:  # locked / vanished file
                files[rel] = {"size": None, "sha256": None, "error": str(e)}
    return {
        "root": str(root),
        "file_count": len(files),
        "total_bytes": sum((v.get("size") or 0) for v in files.values()),
        "files": files,
    }


def save_snapshot(root: Path, label: str, out_dir: Path, timestamp: str | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = snapshot_tree(root)
    snap["label"] = label
    snap["created"] = ts
    out = out_dir / f"snapshot_{ts}_{label}.json"
    out.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def diff_snapshots(before: dict, after: dict) -> dict:
    bf, af = before["files"], after["files"]
    bkeys, akeys = set(bf), set(af)
    changed = sorted(
        k for k in (bkeys & akeys) if bf[k].get("sha256") != af[k].get("sha256")
    )
    return {
        "added": sorted(akeys - bkeys),
        "removed": sorted(bkeys - akeys),
        "changed": changed,
    }


def load_snapshot(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
