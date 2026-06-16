r"""Read/write Dead as Disco ``.bjpl`` playlists (Unreal binary property format).

Format reverse-engineered + validated by byte-perfect round-trip:

  int32(2) byte(0)
  fstr "UniqueID" fstr "StructProperty" i32(1) fstr "Guid" i32(1)
      fstr "/Script/CoreUObject" i32(0) i32(16) byte(8) <16-byte GUID>
  fstr "Name" fstr "StrProperty" i32(0) i32(valsize) byte(0) fstr <name>
  fstr "Songs" fstr "ArrayProperty" i32(1) fstr "ObjectProperty"
      i32(0) i32(arr_valsize) byte(0) i32(count) count*fstr <object path>
  fstr "None"
  i32(0) i32(count) count*( u32 uniqueId, i32 index )

A song is referenced by its ImportedSongs folder name (uEAssetName) wrapped in a
transient object path; the instance numbers in PREFIX are stable across game sessions.
The game populates the playlist from the trailing (uniqueId, index) table -- the hash IS
the song's Meta.json ``uniqueId`` -- so getting those right is what makes songs appear.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

from . import backup, gamestate, meta, paths

PREFIX = ("/Engine/Transient.GameEngine_2147482574:GI_PagodaGameInstance_C_2147482511."
          "PagodaSongCatalogSubsystem_2147482231.")


def playlists_dir() -> Path:
    return paths.saved_dir() / "Playlists"


# --------------------------------------------------------------------------- codec
def _i32(v: int) -> bytes:
    return struct.pack("<i", v)


def _fstr(s: str) -> bytes:
    """UE FString: positive length (incl null) for ASCII, negative for UTF-16LE."""
    if s == "":
        return _i32(0)
    if all(ord(c) < 128 for c in s):
        return _i32(len(s) + 1) + s.encode("ascii") + b"\x00"
    return _i32(-(len(s) + 1)) + s.encode("utf-16-le") + b"\x00\x00"


class _R:
    def __init__(self, b: bytes):
        self.b, self.i = b, 0

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.b, self.i)[0]
        self.i += 4
        return v

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.b, self.i)[0]
        self.i += 4
        return v

    def byte(self) -> int:
        v = self.b[self.i]
        self.i += 1
        return v

    def fstr(self) -> str:
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            raw = self.b[self.i:self.i + n]
            self.i += n
            return raw[:-1].decode("ascii", "replace")
        cnt = -n
        raw = self.b[self.i:self.i + cnt * 2]
        self.i += cnt * 2
        return raw[:-2].decode("utf-16-le", "replace")


def parse(data: bytes):
    """Return (guid_bytes, name, refs, uids)."""
    r = _R(data)
    assert r.i32() == 2 and r.byte() == 0
    assert r.fstr() == "UniqueID" and r.fstr() == "StructProperty"
    assert r.i32() == 1 and r.fstr() == "Guid" and r.i32() == 1
    assert r.fstr() == "/Script/CoreUObject"
    assert r.i32() == 0 and r.i32() == 16 and r.byte() == 8
    guid = data[r.i:r.i + 16]
    r.i += 16
    assert r.fstr() == "Name" and r.fstr() == "StrProperty" and r.i32() == 0
    r.i32()  # name valsize
    assert r.byte() == 0
    name = r.fstr()
    assert r.fstr() == "Songs" and r.fstr() == "ArrayProperty" and r.i32() == 1
    assert r.fstr() == "ObjectProperty" and r.i32() == 0
    r.i32()  # array valsize
    assert r.byte() == 0
    cnt = r.i32()
    refs = [r.fstr() for _ in range(cnt)]
    assert r.fstr() == "None"
    r.i32()
    tn = r.i32()
    uids = []
    for k in range(tn):
        uids.append(r.u32())
        r.i32()  # index
    return guid, name, refs, uids


def serialize(guid: bytes, name: str, refs: list[str], uids: list[int]) -> bytes:
    out = _i32(2) + b"\x00"
    out += (_fstr("UniqueID") + _fstr("StructProperty") + _i32(1) + _fstr("Guid") + _i32(1)
            + _fstr("/Script/CoreUObject") + _i32(0) + _i32(16) + b"\x08" + guid)
    v = _fstr(name)
    out += _fstr("Name") + _fstr("StrProperty") + _i32(0) + _i32(len(v)) + b"\x00" + v
    elems = b"".join(_fstr(x) for x in refs)
    out += (_fstr("Songs") + _fstr("ArrayProperty") + _i32(1) + _fstr("ObjectProperty")
            + _i32(0) + _i32(4 + len(elems)) + b"\x00" + _i32(len(refs)) + elems)
    out += _fstr("None") + _i32(0) + _i32(len(uids))
    for idx, u in enumerate(uids):
        out += struct.pack("<I", u) + _i32(idx)
    return out


def _guid_filename(guid: bytes) -> str:
    a, b, c, d = struct.unpack("<IIII", guid)
    return f"Playlist_{a:08X}{b:08X}{c:08X}{d:08X}.bjpl"


# --------------------------------------------------------------------------- songs
def song_uid(folder: str) -> int | None:
    mj = paths.imported_songs_dir() / folder / "Meta.json"
    if not mj.exists():
        return None
    try:
        data, _ = meta.read_meta(mj)
        return int(data["uniqueId"]) & 0xFFFFFFFF
    except Exception:  # noqa: BLE001
        return None


def song_album_track(folder: str) -> tuple[str | None, float]:
    """(album, track#) from the source file's embedded tags; album None if unknown."""
    mj = paths.imported_songs_dir() / folder / "Meta.json"
    if not mj.exists():
        return None, 0.0
    try:
        data, _ = meta.read_meta(mj)
    except Exception:  # noqa: BLE001
        return None, 0.0
    src = (data.get("originalAudioFilePath") or "").strip()
    album = None
    track = 0.0
    if src and Path(src).exists():
        try:
            from mutagen import File as MF
            f = MF(src, easy=True)
            if f:
                a = (f.get("album") or [None])[0]
                album = a.strip() if a else None
                t = (f.get("tracknumber") or ["0"])[0]
                track = float(str(t).split("/")[0]) if str(t)[:1].isdigit() else 0.0
        except Exception:  # noqa: BLE001
            pass
    if not track:
        head = folder.lstrip()[:3].replace("-", " ").split()
        if head and head[0].isdigit():
            track = float(head[0])
    return album, track


# --------------------------------------------------------------------------- read
def read_all() -> list[dict]:
    out = []
    d = playlists_dir()
    if not d.exists():
        return out
    for f in sorted(d.glob("*.bjpl")):
        try:
            guid, name, refs, uids = parse(f.read_bytes())
        except Exception as e:  # noqa: BLE001
            out.append({"file": f.name, "name": "<parse error>", "leaves": [], "error": str(e)})
            continue
        # leaf = the folder name after PREFIX; strip PREFIX rather than splitting on '.'
        # (folder names can contain dots, e.g. "...figure.09-...").
        leaves = [r[len(PREFIX):] if r.startswith(PREFIX) else r.rsplit(".", 1)[-1] for r in refs]
        out.append({"file": f.name, "path": f, "guid": guid, "name": name, "leaves": leaves})
    return out


def _resolve_guid_filename(name: str) -> tuple[bytes, str]:
    """Reuse an existing same-named playlist's GUID/filename (idempotent), else new."""
    for pl in read_all():
        if pl.get("name") == name and "guid" in pl:
            return pl["guid"], pl["file"]
    guid = os.urandom(16)
    return guid, _guid_filename(guid)


# --------------------------------------------------------------------------- write
def build_blob(name: str, folders: list[str], guid: bytes) -> bytes:
    refs = [PREFIX + f for f in folders]
    uids = []
    for f in folders:
        u = song_uid(f)
        if u is None:
            raise ValueError(f"no uniqueId for song folder: {f}")
        uids.append(u)
    return serialize(guid, name, refs, uids)


def write(name: str, folders: list[str], *, do_backup: bool = True,
          allow_running: bool = False) -> dict:
    if not folders:
        raise ValueError("playlist has no songs")
    if not allow_running:
        procs = gamestate.is_game_running()
        if procs:
            raise RuntimeError(f"game is running ({', '.join(procs)}); refusing to write")
    if do_backup:
        backup.backup_saved(reason=f"playlist '{name}'")
    guid, fname = _resolve_guid_filename(name)
    blob = build_blob(name, folders, guid)
    dest = playlists_dir() / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    verified = dest.read_bytes() == blob
    return {"name": name, "file": fname, "songs": len(folders), "verified": verified}


def delete_by_name(name: str) -> list[str]:
    removed = []
    for pl in read_all():
        if pl.get("name") == name and "path" in pl:
            pl["path"].unlink()
            removed.append(pl["file"])
    return removed
