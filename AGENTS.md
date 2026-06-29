# AGENTS.md — working on dadtool

Context for an AI agent (or human) picking up **dadtool** (repo `dadtool-importer`): an offline
beat-sync + song importer for the rhythm game *Dead as Disco* (Brain Jar Games, Unreal Engine 5,
early access). It analyzes audio **offline** and writes sync/metadata **directly into the game's
per-song save files**, and also imports songs end-to-end, builds playlists, generates custom
challenges, and produces synced `.lrc` lyrics for a companion HUD mod.

Read this first, then `README.md` (user-facing) and `FORMAT.md` (the reverse-engineered save format).

Developer: **Gregory Conroy** — GitHub **@sudravirodhin**.

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

## Workspace & sister project
- **Repo origin:** `github.com/sudravirodhin/dadtool-importer` (public, MIT).
- **Sister project:** **Marquee** HUD mod (repo `dadtool-marquee-hud`, fork of DiscoTracker). dadtool
  is the lyrics **producer**; Marquee is the lyrics **consumer** (display-only). See §Lyrics below.
- Machine-specific workspace paths (working dir, sister repo, deployed mod location) live in `.env`
  (git-ignored). Copy `.env.example` → `.env` and fill in your paths.

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
- **Local tooling:** `gh` CLI (may not be on shell PATH — call via `& "<path>\gh.exe"` in
  PowerShell; path in `.env` as `GH_CLI_PATH`). Auth via Git Credential Manager (GCM).

### Config keys (`dad_config.json` — git-ignored)

All paths are **machine-specific**; the file is never committed. Env var overrides where available:

| Key | Purpose | Env var override |
|-----|---------|------------------|
| `saved_dir` | Game's `Pagoda\Saved` directory | `DAD_SAVED_DIR` |
| `game_install_dir` | Game install root (**currently unused by code** — present for future use) | — |
| `game_exe` | Path to `Pagoda.exe` (for exe-version detection) | — |
| `shipping_exe` | Path to `PagodaSteam-Win64-Shipping.exe` | — |
| `steam_appid` | `3404260` | — |
| `steam_appmanifest` | Path to `appmanifest_3404260.acf` (for build-id detection) | — |
| `beat_this_python` | Python exe in the `dad-beat` conda env | — |
| `acoustid_api_key` | **SECRET** — AcoustID API key for fingerprint lookup | — |
| `fpcalc_path` | Path to the Chromaprint `fpcalc` binary | — |
| `auto_generate_challenge` | `true` → auto-create a Challenge on every import | — |
| `lrcgen_python` | Python exe in the lyrics ASR venv | — |
| `lyrics_cache_dir` | Override for the Marquee mod's lyrics dir | — |
| `lyrics_model` | Whisper model size (`"large-v3"`) | — |
| `lyrics_timing_model` | `"auto"` / `"file"` / `"subtract-start-offset"` | — |
| `auto_generate_lyrics` | `true` → auto-generate lyrics on import | — |
| `soundtrack_dirs` | List of OST directories for built-in song ASR | — |
| `version_baseline` | `{steam_build_id, exe_file_version, captured}` — the validated build | — |

> **Note:** `lyrics.py` derives the lyrics cache dir from `lyrics_cache_dir` (explicit override) or
> `game_install_dir` (auto-derived) in the config. At least one must be set for lyrics commands
> to work.

---

## Architecture (`dadtool/`)
- `cli.py` — command entry point (argparse subcommands).
- `paths.py` — the ONLY place the game dir is resolved.
- `tracker.py` — beat_this subprocess + librosa fallback + a beat cache keyed by audio hash.
- `analyzer.py` — tempo/phase via a gap-robust line fit; drift-criterion section detection; tempo
  floor (smallest integer multiple to reach ≥120 BPM, applied per-section); a noise guard that drops
  bogus sections. `AnalysisResult` dataclass + `from_dict()` classmethod for cache round-tripping.
- `preview.py` — click-track offline preview for ear-checking sync.
- `writer.py` — `AnalysisResult` → `Meta.json`, behind the safety gates (version check, canary,
  backup, verify-by-reread).
- `importer.py` — transcode + fabricate a full `ImportedSongs` entry (no in-game importer needed);
  `reimport()` (swap audio, keep identity/uniqueId); dedup-on-import by source MD5 (`DuplicateSongError`).
- `daemon.py` — the `watch` loop (auto-import from `audio/pending/`); `--limit N` for chunked runs.
- `metadata.py` — AcoustID fingerprint (primary) + embedded tag fallback + cleaned filename;
  `display_label()` returns `"Artist - Title"`.
- `loudness.py` — two-pass ffmpeg loudnorm to −14 LUFS (idempotent).
- `playlist.py` — `.bjpl` Unreal binary codec + per-album auto-grouping.
- `challenge.py` — procedural Challenge generator (spectral profile → waves/bosses/mods/arena).
  Tags sourced from `challenge_vocab.json`.
- `lyrics.py` — synced-lyrics producer for the companion Marquee mod (see below).
- `cache.py` — analysis cache keyed by source-audio hash; entries invalidated by `ANALYZER_VERSION`
  or `FORMAT.md` hash changes.
- `gamestate.py` — game-running detection (process check) + version detection (Steam build id / exe
  version). `refuse_if_running()` helper for the common guard pattern.
- `meta.py` — `Meta.json` read/write with encoding sniffing (UTF-8 / UTF-16LE+BOM by content).
- `backup.py` — timestamped full `Saved` dir backup; verified by file-count + byte-count comparison.
- `overrides.py` — per-song pinned fixes from `overrides.json` (tempo, beatOffset, offsets, sections,
  skip).
- `sources.py` — source-audio filing into `audio/processed/`.
- `snapshot.py` — hash-manifest of the Saved tree for format reverse-engineering.
- `scripts/beat_this_worker.py`, `scripts/lrcgen_worker.py` — the two isolated-env workers; `scripts/revalidate_lyrics.py` — lyrics cache validation tool.

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

## Custom Challenges & Enemy Pools
The custom challenge generator ([challenge.py](file:///F:/dead_as_disco_song_import/dadtool/challenge.py)) uses abstract wave plans mapped to concrete gameplay tags to write custom challenge sidecars (`UserChallenges/<name>/Meta.json`).

### Enemy Tag Harvesting & Verification
Modern UE5 builds bundle assets in IoStore container formats (`.ucas` / `.utoc`). Because UE5 splits name tables into individual token chunks, contiguous gameplay tags must be verified using raw in-memory scanning of `Pagoda-Windows.ucas`.
- **Regular Enemy Pool (`REGULAR_TAGS`):**
  - `Entity.Character.Enemy.Grunt.Mosher` (weight 0.35) — Main melee grunt.
  - `Entity.Character.Enemy.Grunt.Stan.Default` (weight 0.15) — Standard guard grunt.
  - `Entity.Character.Enemy.Grunt.Stan.Low` (weight 0.10) — Low-health stan variant.
  - `Entity.Character.Enemy.Grunt.Echo` (weight 0.10) — High-mobility dodge grunt.
  - `Entity.Character.Enemy.Guard.Shield` (weight 0.12) — Shield-bearing blocker.
  - `Entity.Character.Enemy.Guard.Baton` (weight 0.10) — Baton-equipped melee guard.
  - `Entity.Character.Enemy.Ranged.Fanatic` (weight 0.08) — Range throwing enemy (corrected from previous inference `Grunt.Fanatic` by `.ucas` binary signature analysis).
- **Boss Enemy Pool (`BOSS_TAGS`):**
  - `Entity.Character.Enemy.Boss.Rebel`, `Entity.Character.Enemy.Boss.Doll`, `Entity.Character.Enemy.Boss.Shred`, `Entity.Character.Enemy.Boss.Prophet`.
  - `Entity.Character.Enemy.Boss.BigDoll` (added in UPDATE 1, verified in `.ucas`).
- **Unused/Internal Entities:**
  - `BP_Enemy_Homunculi` / `BT_Enemy_Homunculi` (no confirmed placeable gameplay tag found; used internally).

### Modifier Guidelines
- **DoubleTime:** Speeds up the track and chart.
  - *Safety constraint:* Never apply to fast songs (BPM >= 175). Speeding them up results in 350+ BPM, which is unplayable.
  - *Current logic:* Applied exclusively as a tempo booster to slow/chill songs (BPM < 130) on normal/hard difficulty (difficulty scale >= 1.0) to keep them engaging.

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
- **Pipeline.** Online-first (LRClib duration-matched with an 8-second tolerance, then syncedlyrics multi-provider) → ASR fallback (faster-whisper, **VAD off**, drop stock-phrase hallucinations, drop cross-language lines) → `.miss` if instrumental/unavailable. Built-ins LRClib lacks can be ASR'd from the **OST** (`soundtrack_dirs` config, cached per OST file in `cache/lyrics_ost_asr.json`) or extracted directly from FMOD bank files (`MX_<key>.streams.bank` under `Pagoda/Content/FMOD/Banks/Desktop/`) via `fsb5` and dynamic ctypes patching of the game's native `libogg` and `libvorbis` DLLs.
- **Manifest.** Marquee dumps a full per-load catalog of every song to `…\data\lyrics\_catalog.jsonl` (fields `key/artist/title/songName/durationSec/isImported`). `lyrics._read_queue()` reads it (falls back to the legacy per-session `_requests.jsonl`); `process_queue` filters `not isImported` and skips keys that already have a `.lrc`/`.miss`, so the full manifest self-finds the gaps.
- **Validation.** `scripts/revalidate_lyrics.py` validates the entire `.lrc` cache against duration-matched online sources, with support for auto-overwriting mismatches (like ASR drafts or wrong variants) when run with `--fix`.
- **Transliteration.** Non-Latin lyrics can be automatically transliterated using the `--romaji` flag (or by setting `"transliterate_lyrics": true` in `dad_config.json`). This uses `pykakasi` to convert Japanese Kanji/Kana to Hepburn Romaji, and `anyascii` to convert other non-ASCII languages (e.g. Cyrillic) to standard Latin ASCII characters.
- **Never touch `<key>.offset`** — that is the player's live F9/F10/F11 timing nudge. `--remap` renames
  an orphaned `<oldkey>.{lrc,txt,words.json,offset}` → new key (matched by `[ti:]`/`[ar:]`) to preserve
  nudges/proofing across a built-in key drift; otherwise re-fetch via `--queue`.
- **Manual Proofing & Version Sync.** Output is always a **draft** (ASR mishears sung lyrics, and online databases can contain wrong album variants/arrangements). To improve sync quality:
  1. For songs with complex or differing lyric arrangements (e.g. live, acoustic, deluxe), do not rely on auto-matched online timings. Run ASR model transcription first (`--reference off`) to get audio-accurate timing anchors.
  2. Use online lyrics text as a spelling reference (`--reference "Artist - Title"`) to correct ASR spelling errors and hallucinations while maintaining the ASR's exact timestamps.
  3. The developer/player should manually proof and time-correct the `.lrc` files afterward to ensure perfect alignment.
  Commands:
  `dad lyrics <song>` | `--all [--purge] [--retry-missing] [--romaji]` | `--queue [--limit N]` | `--remap` | `--dry-run`.
- **Marquee crash (sister repo issue #2):** UE4SS `ACCESS_VIOLATION` during gameplay on build
  `CL-29008`. Root cause is the UE4SS loader, NOT Marquee's Lua. Lyrics data works; the mod's display
  is blocked until a newer UE4SS build. Boot-and-quit still populates the catalog manifest. See the
  sister repo's AGENTS.md §5 for the full diagnosis — **do not re-investigate**.

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
  args to native exes — route through Python. Prefer absolute paths. PowerShell calls to `gh.exe`
  must use `& "full\path"` syntax (it's not on PATH).

---

## Repo conventions
- Public, **MIT** licensed. `.gitignore` excludes `dad_config.json` (the key), `audio/`, `backups/`,
  `cache/`, `tools/` (the fpcalc binary), `snapshots/`, `.venv/`, and
  `lyrics_lab/{models,out,backup_existing}` + `reference_lyrics*.txt` (copyright). Keep it that way —
  no secrets, binaries, large downloads, or generated lyrics in the repo.
- **Commits:** end commit messages dynamically with the name of the model/agent currently executing the task, e.g. `Co-Authored-By: <Model Name> <noreply@domain.com>` (or ask the user if the active model/agent name is ambiguous).
- **Issues:** enabled. Use labels for categorization. Reference sister repo issues cross-repo when
  applicable (e.g. `sudravirodhin/dadtool-marquee-hud#2`).
- Companion repo: `dadtool-marquee-hud` (the HUD mod / lyrics consumer; MIT, fork of upstream `hort`).
- Validated against Steam build `23778631` (UPDATE 1 patch, 2026-06-17). `revalidate` then `restore` is the post-patch flow.
- Not affiliated with or endorsed by Brain Jar Games.
