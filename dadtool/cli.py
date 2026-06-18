"""CLI entry point for dadtool — command-line beat-sync & song importer for Dead as Disco."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import gamestate, paths, snapshot

ACOUSTID_RATE_LIMIT_S = 0.34   # AcoustID free tier ~3 req/s


def cmd_status(_args) -> None:
    sd = paths.saved_dir()
    print(f"Saved dir       : {sd}  (exists={sd.exists()})")
    print(f"ImportedSongs   : {paths.imported_songs_dir().exists()}")
    print(f"SaveGames       : {paths.savegames_dir().exists()}")
    running = gamestate.is_game_running()
    print(f"Game running    : {running if running else 'no'}")
    ver = gamestate.detect_version()
    print(f"Game version    : {ver['signal']}={ver['value']} (from {ver['source']})")


def cmd_snapshot(args) -> None:
    sd = paths.saved_dir()
    if not sd.exists():
        raise SystemExit(f"Saved dir does not exist: {sd}")
    out = snapshot.save_snapshot(sd, args.label, paths.SNAPSHOTS_DIR)
    snap = snapshot.load_snapshot(out)
    print(f"Snapshot saved: {out}")
    print(f"  files={snap['file_count']} total_bytes={snap['total_bytes']}")


def cmd_diff(args) -> None:
    before = snapshot.load_snapshot(Path(args.before))
    after = snapshot.load_snapshot(Path(args.after))
    d = snapshot.diff_snapshots(before, after)
    print(json.dumps(d, indent=2, ensure_ascii=False))
    print(f"\nadded={len(d['added'])} removed={len(d['removed'])} changed={len(d['changed'])}")


def _sanitize(name: str) -> str:
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in name).strip()


def _resolve_audio(song: str):
    """Accept a file path or an ImportedSongs folder name; return (audio_path, name)."""
    p = Path(song)
    if p.is_file():
        return p, p.stem
    folder = paths.imported_songs_dir() / song
    ogg = folder / "Audio.ogg"
    if ogg.exists():
        return ogg, song
    raise SystemExit(f"Could not resolve audio for: {song}")


def cmd_analyze(args) -> None:
    from . import analyzer
    audio, _ = _resolve_audio(args.song)
    r = analyzer.analyze(str(audio))
    print(json.dumps(r.to_dict(), indent=2, ensure_ascii=False))


def cmd_preview(args) -> None:
    from . import analyzer, preview
    audio, name = _resolve_audio(args.song)
    r = analyzer.analyze(str(audio))
    final_period = 60.0 / r.final_bpm if r.final_bpm > 0 else r.beat_period_s
    bar_period = r.beat_period_s * 4  # musical bar (pre-floor beat * 4)
    out = paths.REPO_ROOT / "previews" / f"{_sanitize(name)}.preview.wav"
    seconds = None if args.full else args.seconds
    path, sr, dur = preview.make_click_preview(
        str(audio), str(out), final_period, r.first_downbeat_s,
        bar_period_s=bar_period, start_time_s=r.start_time_s, seconds=seconds,
    )
    print(f"detected {r.detected_bpm} BPM x{r.tempo_multiplier} -> {r.final_bpm} | "
          f"downbeat {r.first_downbeat_s}s | conf {r.confidence} | {r.consistency}")
    for f in r.flags:
        print("  flag:", f)
    print(f"preview ({dur}s @ {sr}Hz): {path}")


def cmd_write(args) -> None:
    from . import analyzer, meta, overrides as om, writer
    song = args.song
    mj = paths.imported_songs_dir() / song / "Meta.json"
    if not mj.exists():
        raise SystemExit(f"not an imported song (no Meta.json): {song}")

    # start from any pinned overrides.json entry for this song; CLI flags win
    overrides = dict(om.for_song(song, om.load()))
    if args.tempo is not None:
        overrides["tempo"] = args.tempo
    if args.offset is not None:
        overrides["beatOffset"] = args.offset
    if args.start is not None:
        overrides["startSongOffset"] = args.start
    shown = {k: v for k, v in overrides.items() if k != "notes"}
    if shown:
        print("overrides:", shown)

    result = None
    if not ("tempo" in overrides and "beatOffset" in overrides):
        ogg = paths.imported_songs_dir() / song / "Audio.ogg"
        result = analyzer.analyze(str(ogg))
        print(f"detected {result.detected_bpm} x{result.tempo_multiplier} -> {result.final_bpm} | "
              f"downbeat {result.first_downbeat_s}s | conf {result.confidence} | {result.consistency}")
        for f in result.flags:
            print("  flag:", f)

    if args.dry_run:
        existing, _ = meta.read_meta(mj)
        new = writer.build_meta(existing, result, overrides)
        print("DRY RUN (no write):")
        for k in ("tempo", "beatOffset", "customTempoSections", "startSongOffset"):
            print(f"  {k}: {existing.get(k)!r}  ->  {new.get(k)!r}")
    else:
        r = writer.write_song(song, result, overrides, allow_running=args.hot)
        print("WROTE (verified)" if r["verified"] else "WRITE FAILED VERIFICATION")
        print(json.dumps(r, indent=2, ensure_ascii=False))


def _imported_songs() -> list[str]:
    base = paths.imported_songs_dir()
    return sorted(p.name for p in base.iterdir() if p.is_dir()
                  and (p / "Meta.json").exists() and (p / "Audio.ogg").exists())


def _ns(ad: dict):
    import types
    return types.SimpleNamespace(final_bpm=ad["final_bpm"],
                                 first_downbeat_s=ad["first_downbeat_s"],
                                 start_time_s=ad["start_time_s"],
                                 end_trim_s=ad.get("end_trim_s", 0.0),
                                 bpm_sections=ad.get("bpm_sections", []))


def cmd_batch(args) -> None:
    from . import analyzer, backup, cache as cm, gamestate, meta, overrides as om, writer
    base = paths.imported_songs_dir()
    songs = _imported_songs()
    if args.limit:
        songs = songs[: args.limit]
    print(f"{len(songs)} song(s)")

    if not args.dry_run:
        ok, msgs = writer.preflight()
        for m in msgs:
            print("  preflight:", m)
        if not ok:
            raise SystemExit("preflight failed; not writing")
        print("backup:", backup.backup_saved(reason="batch write"))

    cache = cm.load()
    build = gamestate.detect_version().get("value")
    fmt = cm.format_hash()
    ov_all = om.load()
    flagged, n_cached, n_fresh, n_skip, n_ok, n_fail = [], 0, 0, 0, 0, 0

    for song in songs:
        b = base / song
        meta_dict, _ = meta.read_meta(b / "Meta.json")
        ov = {} if args.no_overrides else om.for_song(song, ov_all)
        if ov.get("skip"):
            n_skip += 1
            print(f"   SKIP {song[:46]}")
            continue
        key = cm.key_for(meta_dict, b / "Audio.ogg")
        entry = cm.get(cache, key)
        if cm.fresh(entry, build, fmt, analyzer.ANALYZER_VERSION) and not args.force:
            ad, src = entry["analysis"], "cache"
            n_cached += 1
        else:
            ad = analyzer.analyze(str(b / "Audio.ogg")).to_dict()
            cm.put(cache, key, song, ad, build, fmt, analyzer.ANALYZER_VERSION)
            src = "fresh"
            n_fresh += 1
        risky = ad["consistency"] == "VARIABLE" or ad["confidence"] < 0.5 or ad.get("above_200_warning")
        if risky:
            flagged.append(song)
        tag = "*" if risky else " "
        if args.dry_run:
            new = writer.build_meta(meta_dict, _ns(ad), ov)
            print(f" {tag}DRY  {song[:42]:42} {str(meta_dict.get('tempo')):>8}->{str(new['tempo']):<8} "
                  f"off {new['beatOffset']:>4} conf {ad['confidence']:.2f} {ad['consistency'][:4]} [{src}]")
        else:
            res = writer.write_song(song, _ns(ad), ov, do_backup=False)
            n_ok += int(res["verified"])
            n_fail += int(not res["verified"])
            print(f" {tag}{'OK ' if res['verified'] else 'FAIL'} {song[:42]:42} tempo {str(res['written']['tempo']):>8} "
                  f"off {res['written']['beatOffset']:>4} conf {ad['confidence']:.2f} {ad['consistency'][:4]} [{src}]")
    cm.save(cache)
    tail = "" if args.dry_run else f" written={n_ok} failed={n_fail}"
    print(f"\nfresh={n_fresh} cached={n_cached} skipped={n_skip}{tail}")
    if flagged:
        print(f"\n{len(flagged)} flagged for preview/review (VARIABLE / low-confidence / >200 BPM):")
        for s in flagged:
            print("   -", s)
        print('Ear-check:  python -m dadtool.cli preview "<song>"')


def cmd_restore(args) -> None:
    from . import backup, cache as cm, meta, overrides as om, writer
    base = paths.imported_songs_dir()
    ok, msgs = writer.preflight()
    for m in msgs:
        print("  preflight:", m)
    if not ok:
        raise SystemExit("preflight failed; not writing")
    if not args.dry_run:
        print("backup:", backup.backup_saved(reason="restore from cache"))
    cache = cm.load()
    ov_all = om.load()
    n_ok = n_nocache = n_skip = n_fail = 0
    for song in _imported_songs():
        b = base / song
        meta_dict, _ = meta.read_meta(b / "Meta.json")
        ov = om.for_song(song, ov_all)
        if ov.get("skip"):
            n_skip += 1
            continue
        entry = cm.get(cache, cm.key_for(meta_dict, b / "Audio.ogg"))
        if not entry:
            n_nocache += 1
            print(f"   no-cache {song[:46]}")
            continue
        if args.dry_run:
            print(f"   would restore {song[:42]:42} -> tempo {entry['analysis']['final_bpm']}")
            continue
        res = writer.write_song(song, _ns(entry["analysis"]), ov, do_backup=False)
        n_ok += int(res["verified"])
        n_fail += int(not res["verified"])
    print(f"\nrestored={n_ok} no_cache={n_nocache} skipped={n_skip} failed={n_fail}")


def cmd_revalidate(args) -> None:
    from . import cache as cm, writer
    vok, vmsg = writer.version_status()
    cok, cmsg = writer.canary_status()
    print("version:", vmsg)
    print("canary :", cmsg)
    print("FORMAT.md hash:", cm.format_hash())
    if vok and cok:
        print("OK: format trusted; safe to write.")
    else:
        print("\nDO NOT WRITE. Re-run Phase 1 AB validation:")
        print("  1) In-game: open a test song's Advanced Editor, set a known Tempo + Beat Offset, save, quit.")
        print("  2) Confirm Meta.json still stores tempo/beatOffset/customTempoSections per FORMAT.md.")
        print("  3) Update FORMAT.md + dad_config.json version_baseline, then re-run.")


def cmd_collect(args) -> None:
    from . import sources
    r = sources.relocate_existing(dry_run=args.dry_run)
    verb = "would move" if args.dry_run else "moved"
    for src, dst in r["moved"]:
        print(f"  {verb}: {src}  ->  {dst}")
    print(f"\n{verb}={len(r['moved'])} already_in_processed={len(r['already'])} missing={len(r['missing'])}")
    for m in r["missing"]:
        print("  source no longer on disk:", m)
    if not args.dry_run and r["moved"]:
        print("\nRun 'batch' next to heal originalAudioFilePath in each Meta.json.")


def cmd_ingest(args) -> None:
    from . import sources
    r = sources.ingest_pending(dry_run=args.dry_run)
    verb = "would move" if args.dry_run else "moved"
    for name, song, _dst in r["moved"]:
        print(f"  {verb}: {name}  (imported in-game as: {song})")
    for u in r["unmatched"]:
        print(f"  still pending (not imported into the game yet): {u}")
    print(f"\nmatched={len(r['moved'])} unmatched={len(r['unmatched'])}")


def cmd_import_song(args) -> None:
    from . import importer
    try:
        r = importer.external_import(args.source, song_name=args.name, artist=args.artist,
                                     allow_running=args.hot, dedupe=not args.allow_duplicate)
    except importer.DuplicateSongError as de:
        raise SystemExit(f"duplicate: this audio is already imported as {de.existing_folder!r} "
                         "(pass --allow-duplicate to import it anyway)")
    print("EXTERNAL IMPORT " + ("(verified)" if r.get("verified") else "(VERIFY FAILED)"))
    print(json.dumps(r, indent=2, ensure_ascii=False))


def cmd_watch(args) -> None:
    from . import daemon
    try:
        daemon.watch(interval=args.interval, do_initial_backup=not args.no_backup, once=args.once,
                     limit=getattr(args, "limit", 0) or 0)
    except KeyboardInterrupt:
        print("\nwatcher stopped.")


def cmd_rename(args) -> None:
    import time
    from . import backup, gamestate, meta, metadata
    running = gamestate.is_game_running()
    if running and not args.dry_run:
        raise SystemExit(f"game is running ({', '.join(running)}); quit it or use --dry-run")
    base = paths.imported_songs_dir()
    if not args.dry_run:
        print("backup:", backup.backup_saved(reason="rename via acoustid"))
    changed = same = nomatch = 0
    for song in _imported_songs():
        mj = base / song / "Meta.json"
        data, enc = meta.read_meta(mj)
        title, artist = metadata.from_acoustid(base / song / "Audio.ogg")
        time.sleep(ACOUSTID_RATE_LIMIT_S)
        if not title:
            nomatch += 1
            continue
        new_name, new_perf = metadata.display_label(title, artist)
        if data.get("songName") == new_name and (data.get("performedBy") or []) == new_perf:
            same += 1
            continue
        print(f"  {data.get('songName')!r}  ->  {new_name!r}   [{song[:28]}]")
        if not args.dry_run:
            data["songName"] = new_name
            data["performedBy"] = new_perf
            meta.write_meta(mj, data, enc)
        changed += 1
    print(f"\n{'would rename' if args.dry_run else 'renamed'}={changed} unchanged={same} no_match={nomatch}")


def cmd_relabel(args) -> None:
    """One-time pass: rewrite songName to '<artist> - <title>' (so the in-game list sorts
    by artist) and blank performedBy. Uses each song's stored artist/title; idempotent."""
    from . import backup, gamestate, meta, metadata
    base = paths.imported_songs_dir()
    songs = _imported_songs()
    running = gamestate.is_game_running()
    if running and not args.dry_run:
        raise SystemExit(f"game is running ({', '.join(running)}); quit it or use --dry-run")
    rows, no_artist = [], 0
    for song in songs:
        mj = base / song / "Meta.json"
        data, enc = meta.read_meta(mj)
        cur = data.get("songName") or ""
        perf = data.get("performedBy") or []
        artist = perf[0] if perf else ""
        if not artist:
            no_artist += 1
        new_name, new_perf = metadata.display_label(cur, artist)
        if new_name == cur and (data.get("performedBy") or []) == new_perf:
            continue
        rows.append((cur, new_name, mj, data, enc))
    print(f"{len(rows)} of {len(songs)} song(s) -> '<artist> - <title>':\n")
    show = rows if args.limit == 0 else rows[: args.limit]
    for cur, new_name, *_ in show:
        print(f"  {cur[:32]:32} ->  {new_name[:48]}")
    if len(show) < len(rows):
        print(f"  ... (+{len(rows) - len(show)} more; --limit 0 for all)")
    if no_artist:
        print(f"\n{no_artist} song(s) have no stored artist (left title-only; run `rename` to fetch one).")
    if args.dry_run:
        print("\ndry-run: nothing written")
        return
    if not rows:
        print("nothing to change")
        return
    print("\nbackup:", backup.backup_saved(reason="relabel artist-first"))
    for cur, new_name, mj, data, enc in rows:
        data["songName"] = new_name
        data["performedBy"] = []
        meta.write_meta(mj, data, enc)
    print(f"\nrelabeled {len(rows)}. Restart the game to see the new artist-sorted order.")


def cmd_normalize(args) -> None:
    import os
    from . import backup, gamestate, loudness, meta
    running = gamestate.is_game_running()
    if running and not args.dry_run:
        raise SystemExit(f"game is running ({', '.join(running)}); quit it or use --dry-run")
    base = paths.imported_songs_dir()
    if not args.dry_run:
        print("backup:", backup.backup_saved(reason="loudness normalize"))
    done = skipped = 0
    for song in (args.songs or _imported_songs()):
        ogg = base / song / "Audio.ogg"
        if not ogg.exists():
            continue
        data, _ = meta.read_meta(base / song / "Meta.json")
        src = Path(data.get("originalAudioFilePath", ""))
        norm_src = src if src.exists() else ogg  # normalize from the original source if present
        try:
            cur = float(loudness.measure(str(ogg))["input_i"])  # current Audio.ogg loudness
            near = abs(cur - args.target) <= 0.5
            if args.dry_run:
                print(f"  {cur:7.1f} LUFS{'  (ok)' if near else f' -> {args.target:.0f}'}   [{song[:40]}]")
                continue
            if near and not args.force:
                skipped += 1
                continue
            tmp = ogg.with_name("Audio.norm.ogg")
            loudness.loudnorm_to_ogg(str(norm_src), str(tmp), target_i=args.target)
            os.replace(str(tmp), str(ogg))
            done += 1
            print(f"  {cur:7.1f} -> {args.target:.0f} LUFS   [{song[:40]}]")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR [{song[:40]}]: {e}")
    if not args.dry_run:
        print(f"\nnormalized {done}, skipped {skipped} (already ~{args.target:.0f} LUFS)")


def cmd_list(args) -> None:
    from . import meta
    base = paths.imported_songs_dir()
    songs = _imported_songs()
    if not songs:
        print("no imported songs")
        return
    print(f"{len(songs)} imported song(s)  (the # is the selector for `remove --index`):\n")
    total = 0
    for i, song in enumerate(songs, 1):
        d = base / song
        try:
            data, _ = meta.read_meta(d / "Meta.json")
        except Exception:  # noqa: BLE001
            data = {}
        name = data.get("songName") or song
        artist = ", ".join(data.get("performedBy") or []) or "?"
        nsec = len(data.get("customTempoSections") or [])
        tempo = float(data.get("tempo", 0) or 0)
        size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
        total += size
        sec = f"{nsec} sec" if nsec else "  -"
        print(f"  {i:>3}. {tempo:6.1f} BPM {sec:>6}  {name[:40]:40}  {artist[:22]}")
    print(f"\n  {total / 1e6:.0f} MB total in {base}")


def _select_songs(args, songs: list[str], base) -> list[str]:
    """Resolve a removal selection from --all / --index / --match / name substrings."""
    import fnmatch

    from . import meta
    selected: set[str] = set()
    if getattr(args, "all", False):
        selected.update(songs)
    if args.index:
        for tok in args.index.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                if "-" in tok:
                    a, b = (int(x) for x in tok.split("-", 1))
                    rng = range(a, b + 1)
                else:
                    rng = [int(tok)]
            except ValueError:
                raise SystemExit(f"bad --index token: {tok!r} (use e.g. 2,4,6-9)")
            for i in rng:
                if 1 <= i <= len(songs):
                    selected.add(songs[i - 1])
                else:
                    print(f"  (ignoring out-of-range index {i})")
    if args.match:
        pat = args.match.lower()
        selected.update(s for s in songs if fnmatch.fnmatch(s.lower(), pat))
    for term in (args.names or []):
        t = term.lower()
        for s in songs:
            if t in s.lower():
                selected.add(s)
                continue
            try:
                data, _ = meta.read_meta(base / s / "Meta.json")
                if t in (data.get("songName") or "").lower():
                    selected.add(s)
            except Exception:  # noqa: BLE001
                pass
    return sorted(selected)


def cmd_remove(args) -> None:
    import shutil

    from . import backup, gamestate, meta
    base = paths.imported_songs_dir()
    songs = _imported_songs()
    if not songs:
        print("no imported songs to remove")
        return
    if not (args.all or args.index or args.match or args.names):
        raise SystemExit("nothing selected. Use `list` first, then `remove` with "
                         "names, --index 2,4,6-9, --match '*pattern*', or --all")

    selected = _select_songs(args, songs, base)
    if not selected:
        print("nothing matched (run `list` to see names / indices)")
        return

    # Capture source paths BEFORE deleting (Meta.json is about to be gone).
    sources: dict[str, Path] = {}
    if args.with_source:
        for s in selected:
            try:
                data, _ = meta.read_meta(base / s / "Meta.json")
                p = (data.get("originalAudioFilePath") or "").strip()
                if p:
                    sources[s] = Path(p.replace("\\", "/"))
            except Exception:  # noqa: BLE001
                pass

    print(f"{len(selected)} of {len(songs)} song(s) selected for removal:")
    for s in selected:
        print(f"   - {s[:62]}")
    if args.with_source:
        print("   (+ their source files under audio/processed/)")

    if args.dry_run:
        print("\ndry-run: nothing deleted")
        return

    procs = gamestate.is_game_running()
    if procs:
        raise SystemExit(f"game is running ({', '.join(procs)}); close it before removing songs")

    if not args.yes:
        try:
            ans = input(f"\nRemove {len(selected)} song(s)? Recoverable only from the backup. [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted")
            return

    # Hard rule: full Saved backup before any modification (makes this undoable).
    print("backup:", backup.backup_saved(reason=f"remove {len(selected)} song(s)"))

    proc_dir = paths.PROCESSED_DIR.resolve()
    removed = failed = src_removed = 0
    for s in selected:
        d = base / s
        try:
            shutil.rmtree(d)
            if d.exists():
                raise RuntimeError("folder still present after delete")
            removed += 1
            print(f"  removed  {s[:54]}")
            sp = sources.get(s)
            if sp and sp.exists() and proc_dir in sp.resolve().parents:
                sp.unlink()
                src_removed += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAILED   {s[:54]}: {e}")
    tail = f", {src_removed} source file(s)" if args.with_source else ""
    print(f"\nremoved {removed} song(s){tail}, failed {failed}")
    if removed:
        print("Restart the game for the change to take effect.")


def _match_songs(terms, songs, base):
    from . import meta
    sel = []
    for s in songs:
        low = s.lower()
        hit = any(t.lower() in low for t in terms)
        if not hit and (base / s / "Meta.json").exists():
            try:
                d, _ = meta.read_meta(base / s / "Meta.json")
                nm = (d.get("songName") or "").lower()
                hit = any(t.lower() in nm for t in terms)
            except Exception:  # noqa: BLE001
                pass
        if hit:
            sel.append(s)
    return sel


def cmd_playlist_list(args) -> None:
    from . import meta, playlist
    base = paths.imported_songs_dir()
    pls = playlist.read_all()
    if not pls:
        print("no playlists")
        return
    print(f"{len(pls)} playlist(s):\n")
    for pl in pls:
        print(f"  {pl['name']}  ({len(pl['leaves'])} songs)")
        for leaf in pl["leaves"]:
            nm = leaf
            mj = base / leaf / "Meta.json"
            if mj.exists():
                try:
                    d, _ = meta.read_meta(mj)
                    nm = d.get("songName") or leaf
                except Exception:  # noqa: BLE001
                    pass
            print(f"      - {nm[:54]}")


def cmd_playlist_create(args) -> None:
    from . import playlist
    base = paths.imported_songs_dir()
    sel = _match_songs(args.terms, _imported_songs(), base)
    if not sel:
        print("no songs matched (run `list` to see names)")
        return
    print(f"playlist {args.name!r} -> {len(sel)} song(s):")
    for s in sel:
        print(f"   - {s[:58]}")
    if args.dry_run:
        print("\ndry-run: not written")
        return
    res = playlist.write(args.name, sel)
    print(("OK " if res["verified"] else "FAIL ") + f"wrote {res['name']!r} ({res['songs']} songs)")
    print("Restart the game to see it.")


def cmd_playlist_delete(args) -> None:
    from . import backup, gamestate, playlist
    if not any(p.get("name") == args.name for p in playlist.read_all()):
        print(f"no playlist named {args.name!r}")
        return
    procs = gamestate.is_game_running()
    if procs:
        raise SystemExit(f"game is running ({', '.join(procs)}); close it first")
    print("backup:", backup.backup_saved(reason=f"delete playlist {args.name}"))
    removed = playlist.delete_by_name(args.name)
    print(f"deleted {len(removed)} file(s). Restart the game to see the change.")


def cmd_playlist_auto(args) -> None:
    from . import backup, gamestate, playlist
    songs = _imported_songs()
    groups: dict[str, list] = {}
    no_album = 0
    for s in songs:
        album, track = playlist.song_album_track(s)
        if not album:
            no_album += 1
            continue
        groups.setdefault(album.lower(), []).append((track, s, album))
    qual = {k: v for k, v in groups.items() if len(v) >= args.min}
    print(f"{len(songs)} songs scanned; {no_album} without a readable album tag")
    print(f"{len(qual)} album(s) with >= {args.min} songs:\n")
    plans = []
    for k in sorted(qual):
        items = sorted(qual[k], key=lambda x: (x[0], x[1]))
        plans.append((items[0][2], [f for _, f, _ in items]))
        print(f"  {items[0][2]}  ({len(items)} songs)")
        for _, f, _a in items:
            print(f"      - {f[:54]}")
    if not plans:
        print("nothing to create")
        return
    if args.dry_run:
        print("\ndry-run: no playlists written")
        return
    procs = gamestate.is_game_running()
    if procs:
        raise SystemExit(f"game is running ({', '.join(procs)}); close it first")
    print("\nbackup:", backup.backup_saved(reason="playlist auto"))
    ok = 0
    for album_name, folders in plans:
        res = playlist.write(album_name, folders, do_backup=False, allow_running=True)
        ok += int(res["verified"])
        print(f"  {'OK ' if res['verified'] else 'FAIL'} {album_name[:38]:38} {res['songs']:>2} songs")
    print(f"\nwrote {ok}/{len(plans)} playlist(s). Restart the game to see them.")


def cmd_challenge_plan(args) -> None:
    from . import challenge, meta
    base = paths.imported_songs_dir()
    songs = _imported_songs()
    matches = [s for s in songs if args.song.lower() in s.lower()]
    if not matches:
        for s in songs:
            try:
                d, _ = meta.read_meta(base / s / "Meta.json")
                if args.song.lower() in (d.get("songName") or "").lower():
                    matches.append(s)
            except Exception:  # noqa: BLE001
                pass
    if not matches:
        raise SystemExit(f"no song matching {args.song!r}")
    folder = matches[0]
    if len(matches) > 1:
        print(f"(multiple matches; using {folder!r})")
    pl = challenge.plan(challenge.profile(folder), args.difficulty)
    print(f"\n{pl['name']}  |  {pl['tempo']:.0f} BPM (x{pl['tempo_factor']} tempo factor)  |  "
          f"song {pl['song_s']:.0f}s  |  intensity {pl['overall_intensity']}  |  diff scale x{pl['scale']}")
    print(f"{len(pl['waves'])} waves (incl. {challenge.LEEWAY_WAVES} leeway); "
          f"est. optimal clear {pl['est_total_s']:.0f}s vs song {pl['song_s']:.0f}s\n")
    print(f"  {'#':>2} {'@s':>4} {'inten':>5}  {'enemies':<20} {'wt':>5} {'clear':>6}")
    for w in pl["waves"]:
        comp = f"{w['regular']} reg" + (f" + {w['bouncer']} Bouncer" if w["bouncer"] else "")
        print(f"  {w['i']:>2} {w['at']:>4} {w['intensity']:>5}  {comp:<20} {w['weight']:>5} "
              f"{w['clear_s']:>5.0f}s{'  (leeway)' if w['leeway'] else ''}")
    print(f"\n  suggested modifiers: {', '.join(pl['mods']) or '(none)'}")
    print("  NOTE: planner only (nothing written). BASE_CLEAR_S is a placeholder until one playtest calibrates it.")


def _resolve_song(term):
    from . import meta
    base = paths.imported_songs_dir()
    songs = _imported_songs()
    for s in songs:
        if term.lower() in s.lower():
            return s
    for s in songs:
        try:
            d, _ = meta.read_meta(base / s / "Meta.json")
            if term.lower() in (d.get("songName") or "").lower():
                return s
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(f"no song matching {term!r}")


def _boss_mode(args):
    return "force" if getattr(args, "boss", False) else ("none" if getattr(args, "no_boss", False) else "auto")


def cmd_challenge_generate(args) -> None:
    from . import challenge, meta
    folder = _resolve_song(args.song)
    sm, _ = meta.read_meta(paths.imported_songs_dir() / folder / "Meta.json")
    name = args.name or challenge.auto_name(sm.get("songName", folder))
    ch, pl = challenge.build_challenge(folder, name, args.difficulty, args.arena, _boss_mode(args))
    total = sum(c for w in ch["enemyWaves"] for c in w["nPCCounts"].values())
    print(f"\n{name!r}  ->  {len(ch['enemyWaves'])} waves, ~{total} enemies, {pl['n_boss']} boss(es)")
    print(f"  arena {pl['arena'].split('.')[-1]}  |  obj WinOnSongEnd  |  mods {pl['mods'] or '(none)'}")
    print(f"  song {pl['song_s']:.0f}s, est. clear {pl['est_total_s']:.0f}s (+boss time), incl. 2 leeway")
    for i, w in enumerate(ch["enemyWaves"], 1):
        comp = ", ".join(f"{k.split('.')[-1].rstrip(chr(34) + ')')}x{v}" for k, v in w["nPCCounts"].items())
        print(f"   wave {i:>2}: {comp}")
    if args.dry_run:
        print("\ndry-run: nothing written")
        return
    res = challenge.write_challenge(name, ch)
    print(("\nOK   " if res["verified"] else "\nFAIL ") + res["file"])
    print("Restart the game — it'll appear in the Challenges tab.")


def cmd_challenge_sync(args) -> None:
    import shutil
    from . import backup, challenge, gamestate, meta
    base = paths.imported_songs_dir()
    songs = _imported_songs()
    if not songs:
        print("no imported songs")
        return
    plans = []
    for folder in songs:
        try:
            sm, _ = meta.read_meta(base / folder / "Meta.json")
            plans.append((folder, challenge.auto_name(sm.get("songName", folder))))
        except Exception:  # noqa: BLE001
            pass
    uc = paths.saved_dir() / "UserChallenges"
    existing_auto = [d.name for d in uc.glob("*") if d.is_dir() and d.name.startswith(challenge.AUTO_PREFIX)] if uc.exists() else []
    print(f"{len(plans)} song(s) -> auto challenges" +
          (f"; would purge {len(existing_auto)} existing 'Auto - ' challenge(s) (hand-made ones untouched)" if args.purge else ""))
    if args.dry_run:
        for folder, name in plans[: (args.limit or 15)]:
            ch, pl = challenge.build_challenge(folder, name, args.difficulty)
            print(f"  {name[:40]:40} {len(ch['enemyWaves'])}w {pl['n_boss']}boss  {pl['arena'].split('.')[-1]:14} mods={pl['mods'] or '-'}")
        if len(plans) > (args.limit or 15):
            print(f"  ... (+{len(plans) - (args.limit or 15)} more)")
        print("\ndry-run: nothing written")
        return
    procs = gamestate.is_game_running()
    if procs:
        raise SystemExit(f"game is running ({', '.join(procs)}); close it first")
    print("backup:", backup.backup_saved(reason="challenge sync"))
    if args.purge:
        for nm in existing_auto:
            shutil.rmtree(uc / nm, ignore_errors=True)
        print(f"purged {len(existing_auto)} existing auto challenge(s)")
    ok = fail = 0
    for folder, name in plans:
        try:
            ch, _ = challenge.build_challenge(folder, name, args.difficulty)
            res = challenge.write_challenge(name, ch, do_backup=False, allow_running=True)
            ok += int(res["verified"])
            fail += int(not res["verified"])
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {name[:38]}: {e}")
    print(f"\nwrote {ok}, failed {fail}. Restart the game to see them in the Challenges tab.")


def cmd_lyrics(args) -> None:
    from . import gamestate, lyrics, meta
    base = paths.imported_songs_dir()
    out = Path(args.out) if args.out else lyrics.cache_dir()
    if not lyrics.mod_installed(out):
        raise SystemExit(f"Marquee mod lyrics dir not found ({out}); install the mod or pass --out <dir>")
    if args.queue:
        if args.dry_run:
            res = lyrics.process_queue(args.out, force=args.force, dry_run=True, limit=args.limit or 0, transliterate=args.romaji)
            print(f"{res['todo']} built-in song(s) to fetch (of {res['requests']} queued):")
            for k, label, st in res["songs"]:
                print(f"  {label:42} {st}")
            print("\n(OST:<file> = will ASR the soundtrack; online-or-miss = try online, else .miss)")
            print("dry-run: nothing written")
            return
        try:
            res = lyrics.process_queue(args.out, force=args.force, limit=args.limit or 0, transliterate=args.romaji)
        except RuntimeError as e:
            raise SystemExit(str(e))
        for k, label, st in res["songs"]:
            print(f"  [{st:4}] {label}  [{k}]")
        outdir = Path(args.out) if args.out else lyrics.cache_dir()
        print(f"\nqueue: {res['ok']} with lyrics (online/OST-ASR), {res['miss']} no-lyrics(.miss), "
              f"{res['requests'] - res['todo']} already had lyrics -> {outdir}")
        print("Restart the game to see built-in lyrics; F9/F10 nudges timing.")
        return
    if args.remap:
        try:
            res = lyrics.remap(args.out, dry_run=args.dry_run)
        except RuntimeError as e:
            raise SystemExit(str(e))
        for row in res["remapped"]:
            extra = f"  ({row[3]})" if len(row) > 3 and row[3] else ""
            print(f"  REMAP {row[2]!r}: {row[0]} -> {row[1]}{extra}")
        tail = "  [dry-run: nothing renamed]" if args.dry_run else ""
        print(f"\nremap: {len(res['remapped'])} renamed, {len(res['unmatched'])} with no orphan"
              f" (run `dad lyrics --queue` to fetch those){tail}")
        return
    if args.purge:
        args.all = True
        args.force = True
    if args.all:
        if args.purge:
            if args.dry_run:
                print(f"would back up + wipe {out}, then regenerate ALL imported songs")
            else:
                procs = gamestate.is_game_running()
                if procs:
                    raise SystemExit(f"game is running ({', '.join(procs)}); close it first")
                pres = lyrics.purge_cache(args.out)
                print(f"purged {pres['removed']} cache file(s); backup: {pres['backup']}")
        todo = []
        for folder in _imported_songs():
            try:
                m, _ = meta.read_meta(base / folder / "Meta.json")
            except Exception:  # noqa: BLE001
                continue
            key = lyrics.song_key(m)
            has_lrc = (out / f"{key}.lrc").exists()
            has_miss = (out / f"{key}.miss").exists()
            if has_lrc and not args.force:
                continue   # real lyrics already present (e.g. fetched online) - don't clobber
            if has_miss and not (args.retry_missing or args.force):
                continue   # marked 'no lyrics'; pass --retry-missing to re-attempt via ASR
            todo.append(folder)
        if args.limit:
            todo = todo[: args.limit]
        if args.force:
            note = ""
        elif args.retry_missing:
            note = " (existing .lrc skipped; retrying .miss songs)"
        else:
            note = " (existing .lrc/.miss skipped; --retry-missing for the misses)"
        print(f"{len(todo)} song(s) -> {out}{note}")
        if args.dry_run:
            for f in todo:
                print("  " + f)
            print("\ndry-run: nothing written")
            return
        procs = gamestate.is_game_running()
        if procs:
            raise SystemExit(f"game is running ({', '.join(procs)}); close it first")
        ok = miss = fail = 0
        for i, folder in enumerate(todo, 1):
            if gamestate.is_game_running():  # re-check each song: a long batch must never write while the game is up
                print(f"  game launched - stopping after {i - 1} done (re-run --all to resume; finished songs skip)")
                break
            try:
                r = lyrics.generate(folder, reference=args.reference, timing=args.timing,
                                    model=args.model, out_dir=args.out, allow_running=True,
                                    transliterate=args.romaji)
                if r.get("instrumental"):
                    miss += 1
                    print(f"  [{i}/{len(todo)}] MISS {folder[:48]}")
                else:
                    ok += 1
                    print(f"  [{i}/{len(todo)}] OK   {folder[:48]} "
                          f"({r['lines']}L {r.get('source')}, {r.get('corrected', 0)} fix)")
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"  [{i}/{len(todo)}] FAIL {folder[:48]}: {str(e)[:80]}")
        print(f"\nlyrics: {ok} written, {miss} instrumental(.miss), {fail} failed -> {out}")
        print("DRAFTS (ASR mishears) - proof the .txt files. Restart the game to load them.")
        return

    if not args.song:
        raise SystemExit("specify a song name, or use --all")
    folder = _resolve_song(args.song)
    if args.dry_run:
        m, _ = meta.read_meta(base / folder / "Meta.json")
        a, t = lyrics._artist_title(m)
        print(f"would generate -> {out / (lyrics.song_key(m) + '.lrc')}  ({a} - {t})")
        return
    r = lyrics.generate(folder, reference=args.reference, timing=args.timing,
                        model=args.model, out_dir=args.out, transliterate=args.romaji)
    if r.get("instrumental"):
        print(f"instrumental: wrote {r['out']} (.miss; no lyrics)")
        return
    shift = f" (-{r['start_off']:.1f}s)" if r["timing"] == "subtract-start-offset" else ""
    print(f"OK  {r['out']}")
    print(f"  {r['lines']} lines | source={r.get('source')} | lang {r.get('language') or 'n/a'} "
          f"| {r.get('corrected', 0)} reconciled | timing={r['timing']}{shift}")
    if r.get("source") == "asr":
        print(f"  ASR DRAFT (mishears expected) - proof: {Path(r['out']).with_suffix('.txt')}")
    else:
        print("  real synced lyrics (online DB), trim-corrected")
    print("  In-game F9/F10 nudges timing live; re-run with --reference off to force ASR.")


def main(argv=None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # so non-ASCII song names print
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="dadtool")
    sub = ap.add_subparsers(required=True, dest="cmd")

    sub.add_parser("status").set_defaults(func=cmd_status)

    sp = sub.add_parser("snapshot")
    sp.add_argument("label", help="label for this snapshot, e.g. 'baseline'")
    sp.set_defaults(func=cmd_snapshot)

    dp = sub.add_parser("diff")
    dp.add_argument("before")
    dp.add_argument("after")
    dp.set_defaults(func=cmd_diff)

    an = sub.add_parser("analyze", help="analyze an audio file or ImportedSongs name")
    an.add_argument("song")
    an.set_defaults(func=cmd_analyze)

    pv = sub.add_parser("preview", help="render a click-track preview WAV")
    pv.add_argument("song")
    pv.add_argument("--seconds", type=float, default=30.0)
    pv.add_argument("--full", action="store_true")
    pv.set_defaults(func=cmd_preview)

    wr = sub.add_parser("write", help="analyze and write a song's Meta.json")
    wr.add_argument("song")
    wr.add_argument("--tempo", type=float, help="override final BPM")
    wr.add_argument("--offset", type=int, help="override beatOffset (ms)")
    wr.add_argument("--start", type=float, help="override startSongOffset (s)")
    wr.add_argument("--dry-run", action="store_true")
    wr.add_argument("--hot", action="store_true", help="allow writing while the game is running (test)")
    wr.set_defaults(func=cmd_write)

    ba = sub.add_parser("batch", help="analyze + write every imported song (uses cache + overrides)")
    ba.add_argument("--dry-run", action="store_true")
    ba.add_argument("--force", action="store_true", help="ignore cache, re-analyze all")
    ba.add_argument("--limit", type=int, default=0)
    ba.add_argument("--no-overrides", action="store_true", help="ignore overrides.json (pure detection)")
    ba.set_defaults(func=cmd_batch)

    rs = sub.add_parser("restore", help="re-write cached metadata back into the game (after a patch)")
    rs.add_argument("--dry-run", action="store_true")
    rs.set_defaults(func=cmd_restore)

    rv = sub.add_parser("revalidate", help="check game version + format canary; force Phase 1 re-check on change")
    rv.set_defaults(func=cmd_revalidate)

    co = sub.add_parser("collect", help="move existing sources (from Meta paths) into audio/processed/")
    co.add_argument("--dry-run", action="store_true")
    co.set_defaults(func=cmd_collect)

    ig = sub.add_parser("ingest", help="match audio/pending/ files to imported songs, move to processed/")
    ig.add_argument("--dry-run", action="store_true")
    ig.set_defaults(func=cmd_ingest)

    im = sub.add_parser("import-song", help="fabricate an ImportedSongs entry from a source file (no in-game import)")
    im.add_argument("source")
    im.add_argument("--name", help="song name (else looked up from tags/filename)")
    im.add_argument("--artist", help="artist (else looked up)")
    im.add_argument("--hot", action="store_true", help="allow while the game is running")
    im.add_argument("--allow-duplicate", action="store_true",
                    help="import even if a byte-identical audio file is already imported")
    im.set_defaults(func=cmd_import_song)

    wt = sub.add_parser("watch", help="auto-import audio dropped into audio/pending/ (daemon)")
    wt.add_argument("--interval", type=float, default=5.0)
    wt.add_argument("--once", action="store_true", help="process current pending once and exit (cron-friendly)")
    wt.add_argument("--no-backup", action="store_true")
    wt.add_argument("--limit", type=int, default=0,
                    help="with --once, stop after N successful imports (for chunked imports)")
    wt.set_defaults(func=cmd_watch)

    rn = sub.add_parser("rename", help="set Song Name/Artist from AcoustID for all imported songs")
    rn.add_argument("--dry-run", action="store_true")
    rn.set_defaults(func=cmd_rename)

    rl = sub.add_parser("relabel", help="rewrite songName to '<artist> - <title>' so the in-game list sorts by artist")
    rl.add_argument("--dry-run", action="store_true")
    rl.add_argument("--limit", type=int, default=40, help="preview rows to show (0 = all)")
    rl.set_defaults(func=cmd_relabel)

    nm = sub.add_parser("normalize", help="LUFS-normalize every Audio.ogg (volume consistency; audio only)")
    nm.add_argument("songs", nargs="*", help="specific song folder(s); default = all")
    nm.add_argument("--target", type=float, default=-14.0, help="integrated LUFS target (default -14)")
    nm.add_argument("--dry-run", action="store_true")
    nm.add_argument("--force", action="store_true", help="re-normalize even if already at target")
    nm.set_defaults(func=cmd_normalize)

    sub.add_parser("list", aliases=["ls"], help="list all imported songs with an index").set_defaults(func=cmd_list)

    rm = sub.add_parser("remove", aliases=["rm"], help="remove imported songs (bulk: by name, --index, --match, or --all)")
    rm.add_argument("names", nargs="*", help="name substrings (matched against folder name and Song Name)")
    rm.add_argument("--index", help="indices from `list`, e.g. 2,4,6-9")
    rm.add_argument("--match", help="glob on the folder name, e.g. '*Sleep Token*'")
    rm.add_argument("--all", action="store_true", help="select every imported song")
    rm.add_argument("--with-source", action="store_true", help="also delete the source file under audio/processed/")
    rm.add_argument("--dry-run", action="store_true", help="show what would be removed, delete nothing")
    rm.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    rm.set_defaults(func=cmd_remove)

    pl = sub.add_parser("playlist", aliases=["pl"], help="manage in-game playlists (.bjpl)")
    plsub = pl.add_subparsers(required=True, dest="plcmd")
    plsub.add_parser("list", help="list playlists and their songs").set_defaults(func=cmd_playlist_list)
    pc = plsub.add_parser("create", help="create a playlist from song-name matches")
    pc.add_argument("name")
    pc.add_argument("terms", nargs="+", help="song name substrings to include")
    pc.add_argument("--dry-run", action="store_true")
    pc.set_defaults(func=cmd_playlist_create)
    pa = plsub.add_parser("auto", help="auto-create a playlist per album with >= N songs")
    pa.add_argument("--min", type=int, default=3, help="minimum songs per album (default 3)")
    pa.add_argument("--dry-run", action="store_true")
    pa.set_defaults(func=cmd_playlist_auto)
    pdl = plsub.add_parser("delete", help="delete a playlist by name")
    pdl.add_argument("name")
    pdl.set_defaults(func=cmd_playlist_delete)

    ch = sub.add_parser("challenge", aliases=["ch"], help="generate custom Challenges (waves) for a song")
    chsub = ch.add_subparsers(required=True, dest="chcmd")
    cp = chsub.add_parser("plan", help="preview a wave/difficulty plan for a song (no write)")
    cp.add_argument("song")
    cp.add_argument("--difficulty", default="auto", choices=["auto", "easy", "normal", "hard"])
    cp.set_defaults(func=cmd_challenge_plan)
    cg = chsub.add_parser("generate", aliases=["gen"], help="generate + write a Challenge for a song")
    cg.add_argument("song")
    cg.add_argument("--name", help="challenge name (default: 'Auto - <song>')")
    cg.add_argument("--difficulty", default="auto", choices=["auto", "easy", "normal", "hard"])
    cg.add_argument("--arena", default=None, help="arena tag (default: picked by song vibe)")
    cg.add_argument("--boss", action="store_true", help="force extra stacked bosses")
    cg.add_argument("--no-boss", action="store_true", help="no bosses")
    cg.add_argument("--dry-run", action="store_true")
    cg.set_defaults(func=cmd_challenge_generate)
    cs = chsub.add_parser("sync", help="generate auto challenges for the whole library")
    cs.add_argument("--difficulty", default="auto", choices=["auto", "easy", "normal", "hard"])
    cs.add_argument("--purge", action="store_true", help="delete existing 'Auto - ' challenges first")
    cs.add_argument("--dry-run", action="store_true")
    cs.add_argument("--limit", type=int, default=0, help="dry-run preview rows (0 = 15)")
    cs.set_defaults(func=cmd_challenge_sync)

    ly = sub.add_parser("lyrics", aliases=["lrc"],
                        help="generate timed .lrc lyrics (faster-whisper ASR) into the Marquee mod cache")
    ly.add_argument("song", nargs="?", help="song name/folder substring (omit when using --all)")
    ly.add_argument("--all", action="store_true",
                    help="process every imported song (skips ones that already have an .lrc/.miss)")
    ly.add_argument("--reference", default="auto",
                    help="auto | off | 'Artist - Title' reference for word correction (default auto)")
    ly.add_argument("--timing", choices=["auto", "file", "subtract-start-offset"],
                    help="auto = per source (ASR->file, online->subtract-offset); or force file | "
                         "subtract-start-offset (default from config: lyrics_timing_model)")
    ly.add_argument("--model", help="whisper model (default large-v3)")
    ly.add_argument("--out", help="output dir (default: the mod's lyrics cache)")
    ly.add_argument("--force", action="store_true", help="with --all, regenerate even if a file exists")
    ly.add_argument("--retry-missing", action="store_true",
                    help="with --all, re-attempt songs marked .miss (e.g. no online lyrics) via ASR")
    ly.add_argument("--purge", action="store_true",
                    help="back up + wipe the whole lyrics cache, then regenerate ALL imported songs "
                         "(implies --all --force)")
    ly.add_argument("--queue", action="store_true",
                    help="fetch lyrics for BUILT-IN songs the mod queued in _requests.jsonl (LRClib); "
                         "imported songs are produced at import, not here")
    ly.add_argument("--remap", action="store_true",
                    help="rename an orphaned <oldkey>.lrc to a newly-queued key when their [ti:]/[ar:] "
                         "match -- preserves F9/F10 .offset + proofing across a key change (then --queue the rest)")
    ly.add_argument("--limit", type=int, help="with --all, cap how many are processed")
    ly.add_argument("--romaji", action="store_true",
                    help="transliterate Japanese Kanji/Kana to Romaji (Hepburn) and other non-ASCII languages to Latin ASCII")
    ly.add_argument("--dry-run", action="store_true")
    ly.set_defaults(func=cmd_lyrics)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
