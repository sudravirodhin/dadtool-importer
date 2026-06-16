r"""Detect whether the game is running and what version it is.

Safety rule: never write to the Saved dir while the game runs.

VERSION SIGNAL (per project constraints): prefer the game exe's file version or
the Steam build id over any save-file timestamp. The install path is stored in
``dad_config.json`` once known; until then version detection reports "unknown"
loudly rather than guessing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import paths

# Process-name fragments that indicate the game is running. "Pagoda" is the UE
# project name (the Saved folder is %LOCALAPPDATA%\Pagoda). Kept specific so it
# does NOT match unrelated processes like "Discord".
GAME_PROC_HINTS = ("pagoda", "deadasdisco", "dead as disco")


def is_game_running() -> list[str]:
    """Return matching running process names (empty list if not running).

    tasklist occasionally returns an empty/truncated list under load; a false "empty"
    here would wrongly report the game closed and allow an unsafe write. So we retry until
    we get a plausibly-full process list before trusting the result.
    """
    out = ""
    for _ in range(3):
        try:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            ).stdout
        except FileNotFoundError:
            return []
        if len([ln for ln in out.splitlines() if ln.strip()]) >= 5:
            break  # got a real process list
    hits: set[str] = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        name = line.split(",")[0].strip('"')
        if any(h in name.lower() for h in GAME_PROC_HINTS):
            hits.add(name)
    return sorted(hits)


def detect_version() -> dict:
    """Return {'signal','value','source'} from a LIVE read each call, so a Steam
    update or game patch is detected immediately. Steam build id is preferred;
    the exe file version (Unreal changelist) is the fallback signal.
    """
    cfg = paths.load_config()

    acf = cfg.get("steam_appmanifest")
    if acf and Path(acf).exists():
        bid = _steam_build_id(Path(acf))
        if bid:
            return {"signal": "steam_build_id", "value": bid, "source": acf}

    exe = cfg.get("game_exe")
    if exe and Path(exe).exists():
        ver = _exe_file_version(Path(exe))
        if ver:
            return {"signal": "exe_file_version", "value": ver, "source": exe}

    return {"signal": "unknown", "value": None, "source": None}


def _steam_build_id(acf: Path) -> str | None:
    import re
    try:
        text = acf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'"buildid"\s*"(\d+)"', text)
    return m.group(1) if m else None


def _exe_file_version(exe: Path) -> str | None:
    # Query the PE file version via PowerShell (no extra Python deps).
    ps = (
        "(Get-Item -LiteralPath '{}').VersionInfo | "
        "ForEach-Object {{ $_.FileVersion }}".format(str(exe).replace("'", "''"))
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        return out or None
    except FileNotFoundError:
        return None
