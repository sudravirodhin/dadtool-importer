"""External import: fabricate an ImportedSongs entry (folder + Audio.ogg +
Meta.json) WITHOUT the in-game importer.

Validated empirically: the game scans the ImportedSongs folder and honors each
Meta.json at launch, so we can skip "Add My Music" entirely. Steps:
  1. transcode the source to 48 kHz Ogg Vorbis (soundfile/libsndfile),
  2. analyze that Audio.ogg through beat_this,
  3. write Meta.json with a fresh uniqueId/seed + analyzed tempo/sections/trims +
     looked-up songName/artist.
The song appears in-game, pre-synced, on the next launch.
"""
from __future__ import annotations

import hashlib
import logging
import random
import shutil
import tempfile
from pathlib import Path

from . import analyzer, backup, gamestate, meta, metadata, paths, writer


def _md5(path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sanitize_folder(name: str) -> str:
    cleaned = "".join("_" if c in '<>:"/\\|?*' else c for c in name).strip().rstrip(".")
    return cleaned or "song"


def transcode_to_ogg(src, dest_ogg, target_sr: int = 48000, normalize: bool = True) -> None:
    """Transcode any source to 48 kHz stereo Ogg Vorbis via the bundled ffmpeg
    (robust across formats; avoids the native libmpg123 MP3-decode crash). When
    `normalize`, applies two-pass LUFS loudness normalization in the same encode."""
    if normalize:
        from . import loudness
        loudness.loudnorm_to_ogg(src, dest_ogg, sr=target_sr)
        return
    import subprocess

    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vn", "-ar", str(target_sr), "-ac", "2", "-c:a", "libvorbis", "-q:a", "5", str(dest_ogg)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not Path(dest_ogg).exists():
        raise RuntimeError(f"ffmpeg transcode failed: {proc.stderr.strip()[-300:]}")


def existing_unique_ids() -> set[int]:
    ids = set()
    base = paths.imported_songs_dir()
    if base.exists():
        for d in base.iterdir():
            mj = d / "Meta.json"
            if mj.exists():
                try:
                    data, _ = meta.read_meta(mj)
                    if isinstance(data.get("uniqueId"), int):
                        ids.add(data["uniqueId"])
                except Exception:  # noqa: BLE001
                    logging.debug("corrupt Meta.json skipped: %s", mj)
    return ids


def _new_unique_id(taken: set) -> int:
    while True:
        v = random.getrandbits(32)
        if v and v not in taken:
            return v


class DuplicateSongError(Exception):
    """Raised by external_import when the source is byte-identical (same MD5) to an
    already-imported song, so we don't create a duplicate ImportedSongs entry."""

    def __init__(self, existing_folder: str, song_hash: str = ""):
        self.existing_folder = existing_folder
        self.song_hash = song_hash
        super().__init__(f"duplicate of already-imported song {existing_folder!r}")


def existing_hashes() -> dict[str, str]:
    """Map {originalAudioFileHash: folder} over imported songs, for dedup-on-import."""
    out: dict = {}
    base = paths.imported_songs_dir()
    if base.exists():
        for d in base.iterdir():
            mj = d / "Meta.json"
            if mj.exists():
                try:
                    data, _ = meta.read_meta(mj)
                    h = data.get("originalAudioFileHash")
                    if h:
                        out[str(h)] = d.name
                except Exception:  # noqa: BLE001
                    logging.debug("corrupt Meta.json skipped: %s", mj)
    return out


def _run_auto_extras(folder: str, song_name: str) -> None:
    """Auto-generate a procedural Challenge and/or DRAFT .lrc if configured.

    Called after both fresh imports and reimports; never fails the caller."""
    cfg = paths.load_config()
    # Auto-generate a procedural Challenge for the song.
    if cfg.get("auto_generate_challenge"):
        try:
            from . import challenge as _ch
            cname = _ch.auto_name(song_name)
            cmeta, _ = _ch.build_challenge(folder, cname)
            _ch.write_challenge(cname, cmeta, do_backup=False, allow_running=True)
        except Exception:  # noqa: BLE001  — never fail the import/reimport
            pass
    # Auto-generate a DRAFT .lrc (ASR; needs human proofing) for the song.
    if cfg.get("auto_generate_lyrics"):
        try:
            from . import lyrics as _ly
            if _ly.mod_installed():            # sister mod: only write if it's installed
                _ly.generate(folder, allow_running=True)
        except Exception:  # noqa: BLE001  — never fail the import/reimport
            pass


def external_import(source_path, song_name=None, artist=None, folder=None,
                    do_backup: bool = True, allow_running: bool = False,
                    dedupe: bool = True) -> dict:
    source_path = Path(source_path)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    running = gamestate.is_game_running()
    if running and not allow_running:
        raise RuntimeError(f"game is running ({', '.join(running)}); refusing to write")

    # Dedup: refuse a byte-identical re-import (same source as an existing song). Checked
    # before any transcode/analysis so it fails fast. `dedupe=False` forces it through.
    src_hash = _md5(source_path)
    if dedupe:
        dup = existing_hashes().get(src_hash)
        if dup:
            raise DuplicateSongError(dup, src_hash)

    lt, la = metadata.lookup(source_path)
    song_name = song_name or lt
    artist = artist if artist is not None else la
    disp_name, disp_perf = metadata.display_label(song_name, artist)  # artist-first label
    folder = folder or _sanitize_folder(source_path.stem)
    dest = paths.imported_songs_dir() / folder
    if dest.exists():
        raise FileExistsError(f"song folder already exists: {dest}")

    backup_dir = backup.backup_saved(reason=f"external import {folder}") if do_backup else None
    dest.mkdir(parents=True)
    ogg = dest / "Audio.ogg"
    transcode_to_ogg(source_path, ogg)

    result = analyzer.analyze(str(ogg))
    base_meta = {
        "version": 1,
        "uniqueId": _new_unique_id(existing_unique_ids()),
        "songName": disp_name,
        "performedBy": disp_perf,
        "writtenBy": [],
        "seed": random.getrandbits(32),
        "tempo": 120,
        "customTempoSections": [],
        "beatOffset": 0,
        "startSongOffset": 0,
        "endSongOffset": 0,
        "uEAssetName": folder,
        "originalAudioFileHash": src_hash,
        "originalAudioFilePath": str(source_path).replace("\\", "/"),
    }
    new = writer.build_meta(base_meta, result, overrides=None)  # fills tempo/offset/sections/trims
    meta.write_meta(dest / "Meta.json", new, "utf-8")

    back, enc = meta.read_meta(dest / "Meta.json")
    verified = all(back.get(k) == new.get(k) for k in
                   ("tempo", "beatOffset", "customTempoSections", "startSongOffset", "endSongOffset"))

    _run_auto_extras(folder, disp_name)

    return {
        "folder": folder, "songName": song_name, "artist": artist,
        "uniqueId": base_meta["uniqueId"], "verified": verified,
        "tempo": new.get("tempo"), "beatOffset": new.get("beatOffset"),
        "sections": len(new.get("customTempoSections", [])),
        "startSongOffset": new.get("startSongOffset"), "endSongOffset": new.get("endSongOffset"),
        "consistency": result.consistency, "confidence": result.confidence,
        "flags": result.flags, "backup": str(backup_dir) if backup_dir else None,
    }


def reimport(source_path, folder, *, do_backup: bool = True,
             allow_running: bool = False) -> dict:
    """Replace an existing ImportedSongs entry's audio + beat-sync from a NEW source,
    PRESERVING its identity (uniqueId, seed, songName, performedBy, writtenBy,
    uEAssetName) so playlists and the in-game entry stay intact. Re-analyzes the new
    audio for tempo/sections/offsets, updates only the source hash/path.

    Order is failure-safe: transcode + analyze the new source FIRST (into a temp), and
    only purge+rewrite the folder once that succeeds; a full Saved backup is the deeper
    net. Verifies by reread and refreshes the auto challenge if configured."""
    source_path = Path(source_path)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    running = gamestate.is_game_running()
    if running and not allow_running:
        raise RuntimeError(f"game is running ({', '.join(running)}); refusing to write")

    dest = paths.imported_songs_dir() / folder
    old_mj = dest / "Meta.json"
    if not old_mj.exists():
        raise FileNotFoundError(f"existing song not found: {old_mj}")
    old, _ = meta.read_meta(old_mj)

    backup_dir = backup.backup_saved(reason=f"reimport {folder}") if do_backup else None

    with tempfile.TemporaryDirectory() as td:
        tmp_ogg = Path(td) / "Audio.ogg"
        transcode_to_ogg(source_path, tmp_ogg)
        result = analyzer.analyze(str(tmp_ogg))
        shutil.rmtree(dest)            # purge the old entry
        dest.mkdir(parents=True)
        shutil.move(str(tmp_ogg), str(dest / "Audio.ogg"))

    base_meta = {
        "version": old.get("version", 1),
        "uniqueId": old["uniqueId"],                       # PRESERVE identity
        "songName": old.get("songName"),
        "performedBy": old.get("performedBy", []),
        "writtenBy": old.get("writtenBy", []),
        "seed": old.get("seed", random.getrandbits(32)),
        "tempo": 120,
        "customTempoSections": [],
        "beatOffset": 0,
        "startSongOffset": 0,
        "endSongOffset": 0,
        "uEAssetName": old.get("uEAssetName", folder),
        "originalAudioFileHash": _md5(source_path),        # NEW audio
        "originalAudioFilePath": str(source_path).replace("\\", "/"),
    }
    new = writer.build_meta(base_meta, result, overrides=None)
    meta.write_meta(dest / "Meta.json", new, "utf-8")

    back, _ = meta.read_meta(dest / "Meta.json")
    verified = (back.get("uniqueId") == old["uniqueId"] and all(
        back.get(k) == new.get(k) for k in
        ("tempo", "beatOffset", "customTempoSections", "startSongOffset", "endSongOffset")))

    _run_auto_extras(folder, old.get("songName") or folder)

    return {
        "folder": folder, "songName": old.get("songName"),
        "uniqueId": old["uniqueId"], "seed": base_meta["seed"], "verified": verified,
        "tempo_old": old.get("tempo"), "tempo": new.get("tempo"),
        "beatOffset": new.get("beatOffset"),
        "sections": len(new.get("customTempoSections", [])),
        "startSongOffset": new.get("startSongOffset"), "endSongOffset": new.get("endSongOffset"),
        "consistency": result.consistency, "confidence": result.confidence,
        "flags": result.flags, "backup": str(backup_dir) if backup_dir else None,
    }
