# Dead as Disco — Save Format Spec

> **STATUS: CONFIRMED by the Phase 1 AB write/round-trip test.** Direct-write to
> `Meta.json` is proven to take effect in-game. Re-validate on any game version
> change (see bottom).

## Game version this was validated against
- Steam appid `3404260`, build id `23332779`
- Exe file version `++brainjar+release-CL-28108`
- In-game UI version string `v0.1.71.0028108-39.4.55`
- Validated 2026-05-30

## Where beat-sync data lives  (CONFIRMED)
- Per-song sidecar: `<Saved>\ImportedSongs\<uEAssetName>\Meta.json`
- Sibling `Audio.ogg` = the transcoded audio the game plays.
- **Not** in the GVAS `.sav` files. All 9 `SaveGames\*.sav` are `GVAS`
  containers; none contain a song's `uniqueId`, name, `tempo` float32, offset, or
  section tempo (probed Overkill's signatures → zero hits). The beat metadata is
  plain JSON: **no checksum, no encryption.**
- `<Saved>\MusicFiles\` is **empty/unused.**

## THE key result: direct-write works  (CONFIRMED)
Wrote `tempo=60, beatOffset=500` to Overkill's `Meta.json` while the game was
closed → launched → the Advanced Editor showed exactly `Tempo: 60`,
`Beat Offset: 500`, and the beat grid rendered at the new phase. Values also
survived a subsequent in-game edit + save. So:
- The game loads `Meta.json` fresh (no authoritative cached copy in the saves).
- `PagodaGP_Main.sav` / `PagodaPT_M_0.sav` change each session for
  progression/session reasons, **not** because they mirror beat data.
- The playable chart is generated at load/play time from `tempo`+`beatOffset`
  (+`seed`), so editing `Meta.json` re-syncs without re-import.

## Meta.json schema  (CONFIRMED)
```json
{
  "version": 1,
  "uniqueId": 2098571242,
  "songName": "Overkill (Acoustic Version) - Colin Hay",
  "performedBy": [],
  "writtenBy": [],
  "seed": 1202075585,
  "tempo": 60,
  "customTempoSections": [
    { "tempo": 90, "startAbsoluteTime": 12.436652183532715 }
  ],
  "beatOffset": 500,
  "startSongOffset": 0,
  "endSongOffset": 0,
  "uEAssetName": "Overkill (Acoustic Version) - Colin Hay",
  "originalAudioFileHash": "769b16b8ccaae97188ad1e79873d9420",
  "originalAudioFilePath": "F:/Downloads/Overkill (Acoustic Version) - Colin Hay.mp3"
}
```

| Field | Type | Units / meaning | Notes |
|---|---|---|---|
| `version` | int | Meta schema version | =1 |
| `uniqueId` | uint32 | per-song id | game-assigned at import |
| `songName` | string | display name | |
| `performedBy` / `writtenBy` | string[] | credits | not auto-filled from filename |
| `seed` | uint32 | chart-generation seed | game-assigned |
| `tempo` | float32 | **BPM** | whole values serialize as int (`60`), fractional as float (`133.6999969482422`). Fresh-import default **120**. Game does NOT auto-detect. |
| `customTempoSections` | array | **BPM Sections** | array of `{tempo: float BPM, startAbsoluteTime: float SECONDS}`, sorted by time. Empty `[]` = single global tempo. Multiple persist (verified 2). |
| `beatOffset` | int | **MILLISECONDS** to the grid's anchor beat | CONFIRMED: `500` → first beat grid line at `0.500 s`. ≥0; larger = first beat later. |
| `startSongOffset` | float | **seconds** trimmed from the START | = "Start Time" (skip intro). |
| `endSongOffset` | float | **seconds** trimmed from the END | UI "End Time" = duration − endSongOffset. `0` = full length. |
| `uEAssetName` | string | = song folder name | |
| `originalAudioFileHash` | md5 hex | hash of the ORIGINAL source file | **stable cache key.** Not the hash of `Audio.ogg`. |
| `originalAudioFilePath` | string | source path at import (forward slashes) | |

### beatOffset model for the writer
The grid has a beat at `t = beatOffset/1000` seconds, repeating every
`60/BPM` seconds. Analyzer's first-downbeat time (s) → `beatOffset = round(t*1000)`.
Existing songs show values 0–362 ms; one (`Devvon`, 196 BPM) exceeds one beat
period, so the game does NOT force beatOffset < one beat — storing the raw
first-downbeat phase in ms is acceptable. (Phase 3 will ear/eye-confirm whether
to store raw first-downbeat ms vs. ms-mod-one-beat.)

### tempo vs. sections (OPEN, low priority)
In one test the global `tempo` (133.7) coexisted with a section `{150 @ 0s}`.
Precedence when a section starts at/near 0 is not yet pinned. For CONSISTENT
songs the tool writes global `tempo` + `customTempoSections: []`, which is
unambiguous. Resolve precedence only when handling VARIABLE songs.

## Serialization style  (match on write)
- Encoding: **UTF-8** for ASCII-only names; **UTF-16LE (BOM)** when the
  name/path contains non-ASCII (Japanese, en-dash, etc.). Sniff the BOM and
  **preserve the per-file encoding** on rewrite.
- **CRLF** line endings, **tab** indent, `": "` key/value separator.
  (`dadtool.meta.write_meta` reproduces this.)

## Gotchas
- **Audio.ogg is re-muxed by the game on editor-save** (its sha256 changed after
  a save that only added sections). The tool never writes `Audio.ogg`; the cache
  keys on `originalAudioFileHash`, not the ogg hash.
- **PowerShell `[bracket]` trap:** song folders like `[DDR] Tsugaru` break
  `Test-Path`/`Join-Path` (wildcards). Tool uses Python for file I/O; PowerShell
  file ops must use `-LiteralPath`.

## VERSION CHANGE RE-VALIDATION
Persist build id + this file's hash in the cache. On each run, read the live
Steam build id (`appmanifest_3404260.acf`) / exe file version. If it changed,
do NOT trust this spec: re-run the Phase 1 AB test (prompt for in-game steps),
confirm or update this file, then resume writing. Treat early-access → stable as
a guaranteed breaking change (full Phase 1 re-run). A `revalidate` command
forces this on demand.
