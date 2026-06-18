# dadtool — offline beat-sync & song importer for *Dead as Disco*

> [!IMPORTANT]
> **AI Assistance Disclosure & Disclaimer**
> This project was developed with the assistance of an AI Large Language Model (LLM). It is a purely personal, non-profit project built for fun to speed up development and make it easier to enjoy the game. If you are not comfortable using software that has been touched or written with the help of AI, please do not proceed.

A Windows CLI that analyzes a track's beat **offline** and writes the sync data
**straight into *Dead as Disco*'s per-song save metadata** — so you never have to use the
in-game tap-calibration tool. It can also import songs end to end (transcode →
auto-named → loudness-normalized → beat-synced), making the in-game importer optional.

The offline grids land roughly **2× tighter than hand-tapping** and don't drift over a
full song.

> [!WARNING]
> **Unofficial and personal.** *Dead as Disco* (Brain Jar Games) is in early access, and
> this reads and rewrites its undocumented save files, which can change with any patch.
> It ships with safety gates — refuses to write while the game is running, backs up the
> whole save folder before every write, checks the game build + a format canary, and
> verifies each write by re-reading — but you use it at your own risk. **Last validated against
> Steam build `23778631`** (Dead as Disco UPDATE 1 patch, 2026-06-17).
> **Windows 11 · Python 3.12.**

## Companion Mod

This tool is designed to work hand-in-hand with the **[Marquee HUD Mod](https://github.com/sudravirodhin/dadtool-marquee-hud)**. While `dadtool` handles offline beat tracking and song importing, Marquee runs in-game to provide synced karaoke lyrics display, career stats tracking, and session leveling.

## What it does

- **Writes beat-sync directly** into each song's `Meta.json` — tempo, beat offset,
  tempo-change sections, and silence trim — replacing manual tap calibration.
- **Imports songs end to end:** transcodes any audio to the game's 48 kHz Ogg Vorbis,
  fabricates the song folder the game expects, and writes a synced `Meta.json`.
- **Tracks tempo accurately** with a transformer beat tracker (beat_this), including true
  downbeats, per-section tempo changes, and automatic half-time-shift correction.
- **Names songs** from an AcoustID audio fingerprint (canonical title/artist via
  MusicBrainz), falling back to file tags + filename.
- **Normalizes loudness** to a consistent −14 LUFS.
- **Runs as a watch daemon** that auto-imports anything dropped into a folder.
- **Plays it safe:** game-running refusal, timestamped full backups, version/format
  gates, verify-by-reread, and a per-song override file for anything it gets wrong.

## Requirements

- **Windows** (uses Windows save paths + process checks) and **Python 3.12**.
- A **separate conda/Miniforge env for the beat tracker** — `beat_this` needs PyTorch
  (CPU-only is fine).
- **ffmpeg is bundled** via `imageio-ffmpeg`; no system install needed.
- *Optional, for auto-naming:* an **AcoustID API key** and the **Chromaprint `fpcalc`**
  binary.

## Setup

```powershell
# 1. main environment
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 2. beat tracker in an isolated conda env (CPU torch is fine)
conda create -n dad-beat python=3.11
conda activate dad-beat
pip install beat_this torch soundfile

# 3. config — copy the example and fill in YOUR paths
copy dad_config.example.json dad_config.json
```

Then edit `dad_config.json`: `saved_dir`, the game exe paths, `beat_this_python` (the
`dad-beat` interpreter), and optionally `acoustid_api_key` + `fpcalc_path`. It's
git-ignored (it holds machine paths + your API key). The game directory is resolved
**only** in `dadtool/paths.py`, via `dad_config.json` or the `DAD_SAVED_DIR` env var.

## Usage

Run through the venv (or the bundled `dad.cmd` / `watch.cmd` launchers):

```
.\.venv\Scripts\python.exe -m dadtool.cli <command>
```

| command | what it does |
|---|---|
| `status` | resolved paths, game-running state, live game version |
| `revalidate` | check game build + format canary before writing |
| `analyze "<song>"` | print analysis JSON for an ImportedSongs name or audio file |
| `preview "<song>"` | render a click-track WAV to `previews/` to ear-check sync |
| `write "<song>"` | analyze + write one song (`--dry-run`, `--tempo/--offset/--start`) |
| `batch` | analyze + write every imported song (`--dry-run`, `--force`, `--limit N`) |
| `import-song <file>` | fabricate a full ImportedSongs entry from a source file |
| `list` | list every imported song with an index (the selector for `remove --index`) |
| `remove` | bulk-delete songs by name, `--index 2,4,6-9`, `--match '*pattern*'`, or `--all` |
| `playlist list` | list playlists and their songs |
| `playlist create "<name>" <terms…>` | create a playlist from song-name matches |
| `playlist auto` | auto-create a playlist per album with 3+ songs (`--min N`, `--dry-run`) |
| `playlist delete "<name>"` | delete a playlist |
| `challenge plan "<song>"` | preview a procedural wave/difficulty plan (no write) |
| `challenge generate "<song>"` | generate + write a Challenge (`--difficulty`, `--boss`/`--no-boss`, `--arena`) |
| `challenge sync` | generate auto challenges for the whole library (`--purge`, `--dry-run`) |
| `watch` | daemon: auto-import audio dropped into `audio/pending/` (`--once`, `--interval N`) |
| `lyrics <song>` \| `--all` \| `--queue [--limit N]` \| `--remap` | synced .lrc lyrics for the Marquee companion mod (`--purge`, `--retry-missing`, `--romaji`, `--dry-run`) |
| `normalize` | LUFS-normalize every `Audio.ogg` to −14 (`--force`) |
| `rename` | set Song Name/Artist from AcoustID for all songs (`--dry-run`) |
| `relabel` | rewrite every Song Name to `Artist - Title` so the in-game list sorts by artist (`--dry-run`) |
| `restore` | re-write cached metadata after a game patch wipes it |
| `collect` / `ingest` | file source audio into `audio/processed/` |
| `snapshot <label>` / `diff <a> <b>` | hash-manifest the save tree + diff (format work) |

### Helper Scripts

- **`scripts/revalidate_lyrics.py`**: Validates cached `.lrc` lyrics in the Marquee mod cache against duration-matched online sources to check for timing and variant mismatches.
  ```powershell
  # Check cache for mismatches
  python scripts/revalidate_lyrics.py

  # Automatically fix mismatched cached lyrics with corrected online synced versions
  python scripts/revalidate_lyrics.py --fix
  ```

**Per-song overrides** live in `overrides.json`, keyed by the exact ImportedSongs folder
name: pin `tempo`, `beatOffset` (ms), `startSongOffset`/`endSongOffset` (s),
`customTempoSections`, or `skip: true`. Applied by `batch` and `write`.

**Managing the library:** `list` prints every song with an index; `remove` clears them in
bulk — `remove --index 2,4,6-9`, `remove --match '*Sleep Token*'`, or `remove --all`. It
previews the selection, **backs up the entire save folder first**, refuses while the game
is running, and takes effect on the next restart. Add `--dry-run` to preview only,
`--yes` to skip the prompt, or `--with-source` to also delete the file in `audio/processed/`.

**Typical flow:** drop a file into `audio/pending/` → `watch` (or `import-song <file>`)
transcodes, analyzes, writes the entry, and files the source into `audio/processed/` →
**restart the game** to see it. Ear-check anything flagged with `preview`; pin fixes in
`overrides.json`. After a game patch: `revalidate`, then `restore`.

> [!NOTE]
> The game reads the imported-song list **once at startup** and caches it for the whole
> session — "Continue" doesn't reload it, and in-game "Add My Music" only appends its one
> song. **New songs and edits appear only after a full game restart.**

---

## How it works

<details>
<summary><b>The save format</b> — what we write, and where</summary>

Each imported song is a folder under `%LOCALAPPDATA%\Pagoda\Saved\ImportedSongs\<name>\`
containing **`Meta.json`** (plain JSON — no checksum or encryption) and **`Audio.ogg`**
(48 kHz Ogg Vorbis). The beat-sync data lives in `Meta.json`, **not** in the GVAS `.sav`
files. Key fields:

| field | meaning |
|---|---|
| `tempo` | BPM (float32; whole numbers stored as `int`, like the game) |
| `beatOffset` | grid anchor, in **milliseconds** (int) |
| `customTempoSections` | `[{tempo, startAbsoluteTime(seconds)}]` — tempo changes over time |
| `startSongOffset` / `endSongOffset` | seconds trimmed from start / end |
| `uniqueId` / `seed` | uint32 ids |
| `songName` / `performedBy` | display name + credits |
| `originalAudioFileHash` / `originalAudioFilePath` | MD5 + path of the source |

Encoding is UTF-8 for ASCII names, **UTF-16LE + BOM** for non-ASCII names; CRLF line
endings with tab indent. The full reverse-engineered spec is in **[FORMAT.md](FORMAT.md)**.

</details>

<details>
<summary><b>The analysis pipeline</b> — beats → tempo → phase</summary>

1. **beat_this** (CPJKU, 2024 — a SOTA transformer tracker) produces beats **and true
   downbeats**, run as a subprocess inside the isolated conda env
   (`scripts/beat_this_worker.py`). `librosa` is the in-process fallback.
2. Tempo and phase come from a **gap-robust line fit** over the beat sequence:
   inter-beat intervals are rounded to whole grid steps, so a skipped or extra beat from
   the tracker doesn't skew the slope. This yields a tempo + downbeat phase that holds
   across the whole song without drift (~11–25 ms residual on clean tracks).
3. The result carries detected/final BPM, the tempo multiplier, the first downbeat,
   tempo sections, a residual-driven confidence score, and review flags.

Beat tracks are cached by audio hash, so re-analysis is fast.

</details>

<details>
<summary><b>Tempo floor &amp; per-section doubling</b></summary>

The game plays best at ≥120 BPM, so a slow detected tempo is multiplied by the
**smallest integer** that reaches 120 (90→180, 100→200). Because it's an integer
multiple, every original beat is still a beat — the grid stays aligned.

For songs with tempo sections, each section is floored **independently**: a half-time
stretch the tracker caught inside a fast song (e.g. an 87-BPM bridge in a 175-BPM song)
gets doubled to ~174 so it doesn't crawl, while sections already in range are left alone.
A single song-wide multiplier can't do this — a fast song's global tempo is already ≥120
(×1), so its slow sections would never get lifted. Lifting can push a section past 200
BPM; that's intentional (a too-slow shift is worse than a fast one).

</details>

<details>
<summary><b>Section detection &amp; the noise guard</b></summary>

Sections come from **drift-criterion segmentation**: the local tempo curve is segmented,
and a piecewise grid is kept only if it cuts the worst beat drift by more than ~15 ms
versus a single tempo. Isolated one-segment spikes are rejected. Steady songs stay
single-tempo; genuinely varying songs get accurate multi-section grids (which *reduce*
drift — they're a feature, not a problem).

**The noise guard:** if even the sectioned grid still drifts badly (>400 ms) *and* barely
improves over a single tempo (<20%), the sections are discarded and the song falls back
to the robust global tempo. That pattern means the tracker mis-*placed* beats (e.g. dense
vocals over sparse percussion) and the "sections" were fitting noise, not real tempo
changes. Notably, the confidence score alone does **not** separate good sections from bad
— the *improvement ratio* does.

</details>

<details>
<summary><b>End-to-end import &amp; the startup-cache quirk</b></summary>

The game discovers songs by scanning `ImportedSongs\` and reading each `Meta.json`
**at launch** — there's nothing song-specific in the GVAS saves. So the tool fabricates
the whole entry itself: transcode the source to 48 kHz Ogg Vorbis (using bundled ffmpeg —
`librosa`/`soundfile` decoding of source MP3s stack-overflows on Windows), generate
`uniqueId`/`seed`, analyze, and write `Meta.json`.

The song list is **cached at startup** for the entire session (confirmed by testing):
nothing re-reads the folder mid-session, "Continue" doesn't reload it, and in-game "Add My
Music" only appends its one song. So externally-added songs and edits show up only after a
**full game restart**.

</details>

<details>
<summary><b>Naming, loudness &amp; safety gates</b></summary>

- **Naming:** an AcoustID fingerprint is looked up against MusicBrainz and
  **majority-voted** across the fingerprint's recordings (avoids spurious single matches),
  falling back to embedded tags (`mutagen`) then a cleaned filename. The in-game **Song
  Name** is written as `Artist - Title` (artist field blanked) so the menu — which sorts
  lexicographically by name — groups by artist; `relabel` applies this to the whole
  library and new imports get it automatically.
- **Loudness:** ffmpeg two-pass `loudnorm` to −14 LUFS, idempotent (it measures first and
  skips songs already on target).
- **Safety gates before any write:** refuse if the game is running (`Pagoda.exe` /
  `PagodaSteam-Win64-Shipping.exe`); timestamped full backup of the entire Saved dir;
  game-build check against the recorded baseline (demands re-validation on change); a
  format canary on a known song; and verify-by-reread.

</details>

<details>
<summary><b>Playlists (.bjpl)</b></summary>

Playlists live in `Saved\Playlists\Playlist_<GUID>.bjpl` — Unreal's **binary property
format**, one file per playlist (reverse-engineered + validated by byte-perfect
round-trip; the codec is in `dadtool/playlist.py`). Each references songs by their
ImportedSongs folder name wrapped in a transient object path (whose instance numbers are
stable across sessions), and carries a trailing table mapping each song's **`uniqueId`**
(the value we already write into `Meta.json`) to its order. The game populates the
playlist from that uniqueId table — get the ids right and the songs appear. `playlist
auto` reads each song's **album** from embedded tags and writes one playlist per album
that has 3+ songs.

</details>

<details>
<summary><b>Procedural challenges (UserChallenges)</b></summary>

The game's custom-fight editor stores challenges as plain JSON in
`Saved\UserChallenges\<name>\Meta.json` (embedded song sync + `enemyWaves` + `objectives` +
`modAssets` + arena). `dadtool` generates them procedurally from the song:

- A spectral **intensity profile** sizes each wave; the wave count is bounded (~8.5 s/wave)
  so long songs get bigger waves, not dozens of tiny ones, plus a 2-wave end buffer.
- **Clear-time model** (calibrated by playtest): regular enemies ×1, Bouncer ×2.5, bosses ×5,
  scaled by tempo. The game's takedown-token system self-regulates on-screen pressure, so
  generous over-provisioning stays intense-but-fair.
- **By vibe:** intensity/tempo pick the **arena**, the **modifiers** (e.g. DoubleTime,
  EnemyIncreasedHealth), and how many **bosses** stack into the finale — brutal songs get
  ludicrous boss stacks, by design.
- Objective is `WinOnSongEnd` (survive the song). Enemy/arena/mod tags were harvested from the
  packed game content into `challenge_vocab.json`.

`challenge generate` writes one; `challenge sync --purge` regenerates the whole library; and with
`auto_generate_challenge: true` in `dad_config.json`, every new import gets one automatically.

</details>

<details>
<summary><b>Lyrics &amp; Companion Mod integration</b> — online fetch, duration-matching, FMOD bank extraction, and validation</summary>

The synced lyrics pipeline generates `.lrc` lyrics for the **Marquee companion HUD mod**:

- **Duration-Matched Online Fetch**: Requests synced lyrics from LRClib within a strict tolerance (default `8` seconds). This prevents variant mismatches (e.g. matching an extended/radio mix or transition variants like *Points of Authority* which have different durations).
- **FMOD Bank Audio Extraction Fallback**: If a built-in game song is not available online, `dadtool` can extract its audio stream directly from the game's desktop bank files (`MX_<key>.streams.bank` under `Pagoda/Content/FMOD/Banks/Desktop/`).
  - Pre-loads the game's built-in Ogg and Vorbis DLLs (`libogg_64.dll` and `libvorbis_64.dll`) from the engine binaries directory (`Engine/Binaries/ThirdParty/`) using dynamic ctypes patching.
  - Rebuilds and extracts the raw Vorbis audio stream from FMOD's `FSB5` container using the `fsb5` library without requiring system-wide audio package dependencies.
  - Feeds the extracted file to `faster-whisper` ASR to generate a timed `.lrc` draft.
- **Romaji & ASCII Transliteration**: With the `--romaji` flag (or by setting `"transliterate_lyrics": true` in `dad_config.json`), non-Latin lyrics (like Japanese Hiragana/Katakana/Kanji and Russian Cyrillic) are automatically transliterated. Japanese text is converted to readable Hepburn Romaji (using `pykakasi`), and other non-ASCII languages are transliterated to Latin ASCII (using `anyascii`).
- **Lyrics Revalidation Tool**: You can revalidate the entire cache of cached `.lrc` files against duration-matched online sources using `scripts/revalidate_lyrics.py`.

</details>

<details>
<summary><b>Windows gotchas</b> (learned the hard way)</summary>

- Every subprocess call to ffmpeg / the beat worker passes
  `encoding="utf-8", errors="replace"` — the default cp1252 reader thread crashes on
  UTF-8 stderr when a filename has non-ASCII (e.g. Japanese) characters, silently
  returning nothing.
- PowerShell mangles `[bracket]` paths in `Test-Path`/`Join-Path` and garbles non-ASCII
  arguments to native exes; the tool sidesteps both by working through Python.
- `Meta.json` encoding is chosen by **content**: any non-ASCII character → UTF-16LE + BOM
  (the game's own convention); pure ASCII → UTF-8. A non-ASCII name saved as no-BOM UTF-8
  gets misread as the ANSI codepage in-game (`Don’t` → `Donâ€™t`).

</details>

## Project layout

```
dadtool/                 the package
  cli.py                 command-line entry point
  paths.py               resolves the game dir (the ONLY place the game path lives)
  tracker.py             beat_this subprocess + librosa fallback + beat cache
  analyzer.py            tempo/phase line-fit, sections, tempo floor, noise guard
  writer.py              AnalysisResult -> Meta.json, with the safety gates
  importer.py            transcode + fabricate a full ImportedSongs entry
  daemon.py              the watch loop
  metadata.py            AcoustID + tag naming
  loudness.py            two-pass loudnorm
  playlist.py            .bjpl playlist codec + album auto-grouping
  challenge.py           procedural Challenge generator (spectral -> waves/bosses/mods/arena)
  lyrics.py              synced-lyrics (.lrc) producer for the companion Marquee mod (draft — requires human proofing)
  meta / snapshot / backup / overrides / sources / gamestate
scripts/
  beat_this_worker.py    beat tracker — runs inside the isolated conda env
  lrcgen_worker.py       lyrics ASR (faster-whisper) — runs inside an isolated venv
FORMAT.md                reverse-engineered save format (source of truth for writes)
dad_config.example.json  copy to dad_config.json and fill in
overrides.json           per-song pinned fixes
```

## Caveats

- Reverse-engineered against a single early-access build; a patch can change the format.
  `revalidate` checks for this, and `restore` re-applies sync after a wipe.
- The beat tracker is the accuracy ceiling. Rare mis-tracks are flagged (low confidence)
  for `preview` + a per-song override — there's no acoustic signal for everything (e.g.
  "should this be charted double-time?" is a charting taste call, not in the audio).
- `madmom` was evaluated but has no Windows wheel / conda-forge build; beat_this is more
  accurate and pip-installable.

## License

Released under the **MIT License** — see [LICENSE](LICENSE). *Dead as Disco* is a
trademark of Brain Jar Games; this project is not affiliated with or endorsed by them.

## Credits

[beat_this](https://github.com/CPJKU/beat_this) (CPJKU) · librosa · ffmpeg (via
imageio-ffmpeg) · [AcoustID](https://acoustid.org) + Chromaprint · MusicBrainz.
