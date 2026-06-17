# AGENTS.md — working on dadtool

Context for an AI agent (or human) picking up **dadtool** (repo `dadtool-importer`): an offline
beat-sync + song importer for the rhythm game *Dead as Disco* (Brain Jar Games, Unreal Engine 5,
early access). It analyzes audio **offline** and writes sync/metadata **directly into the game's
per-song save files**, and also imports songs end-to-end, builds playlists, generates custom
challenges, and produces synced `.lrc` lyrics for a companion HUD mod.

Read this first, then `README.md` (user-facing) and `FORMAT.md` (the reverse-engineered save format).

---

## Non-negotiable rules (safety)
This tool rewrites an **undocumented, live game save**. These are hard constraints, not preferences:

1. **Direct-write is the whole point.** Beat-sync goes straight into each song's `Meta.json`. Do not
   pivot to an indirect/in-game-only approach.
2. **Never write to the game directory while the game is running.** Check first — `Pagoda.exe` /
   `PagodaSteam-Win64-Shipping.exe` via `tasklist`/`Get-Process` (`dadtool/gamestate.py`) — and refuse
   if running.
3. **Back up the entire `Saved` dir before every write**, to a timestamped folder under `backups/`
   (git-ignored). **Never prune backups across a game version boundary.** (`dadtool/backup.py`.)
4. **Verify every write by re-reading** the file and comparing the fields you wrote.
5. **On any game update, writes are gated.** `dad revalidate` checks the build id + a format canary.
   Re-run a Phase-1 A/B test (set a known tempo/offset in-game, save, confirm `Meta.json` still matches
   `FORMAT.md`) and update `version_baseline` in the config before writing again.
6. **The human is your hands in the game.** Anything in-game — playing a song, the Advanced Editor,
   watching the HUD — is theirs to do. Give exact numbered steps and wait.
7. **Secrets & copyright.** `dad_config.json` holds the AcoustID API key and machine paths — it is
   git-ignored; never commit or echo it. Generated **lyrics are copyrighted**: produced per-user into
   the game dir, never bundled or committed (`lyrics_lab/` outputs are git-ignored).

---

## Environment / how to run
- **Windows 11, Python 3.12.** The game dir is resolved **only** in `dadtool/paths.py` (via
  `dad_config.json` or the `DAD_SAVED_DIR` env var); default `%LOCALAPPDATA%\Pagoda\Saved`.
- **Main venv** (`.venv`): `pip install -r requirements.txt` (numpy, librosa, soundfile,
  imageio-ffmpeg [bundled ffmpeg, no system install], pyacoustid, mutagen, syncedlyrics).
- **Beat tracker** runs in a separate **conda env** (`dad-beat`: py3.11 + torch CPU + `beat_this`),
  called as a subprocess via `dadtool/tracker.py` → `scripts/beat_this_worker.py` (path in config key
  `beat_this_python`). `librosa` is the in-process fallback.
- **Lyrics ASR** runs in an **isolated venv** (`faster-whisper`, no torch), subprocess via
  `dadtool/lyrics.py` → `scripts/lrcgen_worker.py` (config key `lrcgen_python`). The large-v3 model is
  cached under `lyrics_lab/models` (git-ignored).
- Run with `python -m dadtool.cli <command>` (or the `dad.cmd` / `watch.cmd` launchers). Copy
  `dad_config.example.json` → `dad_config.json` and fill in paths/keys.

---

## Architecture (`dadtool/`)
- `cli.py` — command entry point (argparse subcommands).
- `paths.py` — the ONLY place the game dir is resolved.
- `tracker.py` — beat_this subprocess + librosa fallback + a beat cache keyed by audio hash.
- `analyzer.py` — tempo/phase via a gap-robust line fit; drift-criterion section detection; tempo
  floor (smallest integer multiple to reach ≥120 BPM, applied per-section); a noise guard that drops
  bogus sections.
- `writer.py` — `AnalysisResult` → `Meta.json`, behind the safety gates (version check, canary,
  backup, verify-by-reread).
- `importer.py` — transcode + fabricate a full `ImportedSongs` entry (no in-game importer needed);
  `reimport()` (swap audio, keep identity/uniqueId); dedup-on-import by source MD5 (`DuplicateSongError`).
- `daemon.py` — the `watch` loop (auto-import from `audio/pending/`); `--limit N` for chunked runs.
- `metadata.py` — AcoustID fingerprint + tag naming; `display_label()` returns `"Artist - Title"`.
- `loudness.py` — two-pass ffmpeg loudnorm to −14 LUFS (idempotent).
- `playlist.py` — `.bjpl` Unreal binary codec + per-album auto-grouping.
- `challenge.py` — procedural Challenge generator (spectral profile → waves/bosses/mods/arena).
- `lyrics.py` — synced-lyrics producer for the companion Marquee mod (see below).
- `meta / snapshot / backup / overrides / sources / gamestate` — helpers.
- `scripts/beat_this_worker.py`, `scripts/lrcgen_worker.py` — the two isolated-env workers.

---

## Reverse-engineered formats (full spec in `FORMAT.md`)
- **Imported songs:** `…\Pagoda\Saved\ImportedSongs\<folder>\` = `Meta.json` (plain JSON: `tempo`,
  `beatOffset` (ms), `customTempoSections`, `start/endSongOffset`, `uniqueId`, `seed`, `songName`,
  `performedBy`, `originalAudioFileHash`/`Path`, `uEAssetName`) + `Audio.ogg` (48 kHz Vorbis). The game
  reads the song list **once at startup** — new songs/edits need a full restart.
  **Encoding by content:** any non-ASCII → UTF-16LE+BOM (the game's convention); pure ASCII → UTF-8.
- **Playlists:** `Saved\Playlists\Playlist_<GUID>.bjpl` — Unreal binary property format; song ref =
  transient-path prefix + folder name; a trailing table maps each song's `uniqueId` → order.
- **Challenges:** `Saved\UserChallenges\<name>\Meta.json` — plain JSON (embedded song meta +
  `enemyWaves` + `objectives` + `modAssets` + arena). Enemy/arena/mod tags harvested into
  `challenge_vocab.json`.
- **Lyrics:** standard LRC, `[mm:ss.xx]` timestamps, UTF-8 **no BOM** (a BOM breaks the `[ti:]` tag).

---

## Lyrics + the Marquee sister mod
dadtool is the lyrics **producer**; the **Marquee** HUD mod (companion repo `dadtool-marquee-hud`, a
UE4SS Lua mod) is the **consumer**. Contract:
- Write `<key>.lrc` into the mod cache: `…\Pagoda\Binaries\Win64\ue4ss\Mods\Marquee\Scripts\data\lyrics\`.
  `<key>` = `lyrics.song_key()` — imported → `uniqueId`; built-in → asset short-name — matching the
  mod's `cleanKey()`.
- **Timing.** Transcribe the *shipped, trimmed* `Audio.ogg` → already 0-based (model `file`, no shift).
  Online (LRClib/syncedlyrics) lyrics are timed to the original release → subtract `startSongOffset`
  (model `subtract-start-offset`). Config `lyrics_timing_model: auto` picks per source.
- **Pipeline.** Online-first (LRClib duration-matched, then syncedlyrics multi-provider) → ASR fallback
  (faster-whisper, **VAD off**, drop stock-phrase hallucinations, drop cross-language lines) → `.miss`
  if instrumental/unavailable. Built-ins LRClib lacks but that exist in the **OST** are ASR'd from
  `soundtrack_dirs` (config), cached per OST file in `cache/lyrics_ost_asr.json`.
- **Manifest.** Marquee dumps a full per-load catalog of every song to `…\data\lyrics\_catalog.jsonl`
  (fields `key/artist/title/songName/durationSec/isImported`). `lyrics._read_queue()` reads it (falls
  back to the legacy per-session `_requests.jsonl`); `process_queue` filters `not isImported` and skips
  keys that already have a `.lrc`/`.miss`, so the full manifest self-finds the gaps.
- **Never touch `<key>.offset`** — that is the player's live F9/F10/F11 timing nudge. `--remap` renames
  an orphaned `<oldkey>.{lrc,txt,words.json,offset}` → new key (matched by `[ti:]`/`[ar:]`) to preserve
  nudges/proofing across a built-in key drift; otherwise re-fetch via `--queue`.
- Output is always a **draft** (ASR mishears sung lyrics); the human proofs. Commands:
  `dad lyrics <song>` | `--all [--purge] [--retry-missing]` | `--queue [--limit N]` | `--remap` | `--dry-run`.

---

## Operational learnings (read before long-running work)
- **Long background tasks die ~18–20 min in** (an environment reaper; cause unconfirmed, but NOT PC
  sleep). **Chunk everything:** `lyrics --all` is resumable (skips done), `watch --once --limit N`,
  `lyrics --queue --limit N`. An *unclean* death does **not** send a completion signal — verify state
  (file/count) rather than trusting silence. The runtime also auto-backgrounds any multi-minute
  "foreground" command, so chunking is the reliable pattern.
- **Don't batch dense/long ASR tracks carelessly** — some (e.g. orchestral, vocaloid) can hang the
  worker up to its 30-min timeout and stall a whole chunk. The OST-file ASR cache makes re-runs cheap.
- **UE4SS mods break on game updates.** After a patch the Marquee HUD (and every UE4SS mod) may show
  nothing until UE4SS is updated for the new build — diagnose via `…\ue4ss\UE4SS.log` (look for failed
  `FName::FName` / `FUObjectHashTables` AOB scans). dadtool's lyric data being correct ≠ the mod
  displaying it; check both sides.
- **Windows gotchas:** every subprocess call passes `encoding="utf-8", errors="replace"` (the default
  cp1252 reader thread crashes on non-ASCII paths). PowerShell mangles `[bracket]` paths and non-ASCII
  args to native exes — route through Python. Prefer absolute paths.

---

## Repo conventions
- Public, **MIT** licensed. `.gitignore` excludes `dad_config.json` (the key), `audio/`, `backups/`,
  `cache/`, `tools/` (the fpcalc binary), `snapshots/`, `.venv/`, and
  `lyrics_lab/{models,out,backup_existing}` + `reference_lyrics*.txt` (copyright). Keep it that way —
  no secrets, binaries, large downloads, or generated lyrics in the repo.
- Companion repo: `dadtool-marquee-hud` (the HUD mod / lyrics consumer; MIT, fork of upstream `hort`).
- Validated against Steam build `23332779`; *Dead as Disco* UPDATE 1 is build `23726858` (writes stay
  gated until re-validated — see rule 5). `revalidate` then `restore` is the post-patch flow.
- Not affiliated with or endorsed by Brain Jar Games.
