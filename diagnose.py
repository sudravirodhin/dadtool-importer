import sys
import json
import re
from pathlib import Path

# Add the repo directory to path to use dadtool modules
sys.path.append("F:/dead_as_disco_song_import")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dadtool import paths, meta, cache as cm, analyzer, writer, lyrics, gamestate
from dadtool.metadata import _is_acoustid_plausible, from_tags, _extract_keywords

def get_imported_songs() -> list[str]:
    base = paths.imported_songs_dir()
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir()
                  and (p / "Meta.json").exists() and (p / "Audio.ogg").exists())

def main():
    print("=" * 80)
    print("                     DEAD AS DISCO IMPORTED SONGS DIAGNOSTICS")
    print("=" * 80)
    
    songs = get_imported_songs()
    if not songs:
        print("No imported songs found.")
        return
        
    print(f"Analyzing {len(songs)} song(s) in the library...\n")
    
    # Load analysis cache
    analysis_cache = cm.load()
    build = gamestate.detect_version().get("value")
    fmt = cm.format_hash()
    
    flagged = []
    
    for i, folder in enumerate(songs, 1):
        folder_path = paths.imported_songs_dir() / folder
        meta_file = folder_path / "Meta.json"
        audio_file = folder_path / "Audio.ogg"
        
        # Read current metadata
        m, enc = meta.read_meta(meta_file)
        
        # Get analysis details
        key = cm.key_for(m, audio_file)
        entry = cm.get(analysis_cache, key)
        
        is_cached = True
        if entry and cm.fresh(entry, build, fmt, analyzer.ANALYZER_VERSION):
            ad = entry["analysis"]
        else:
            is_cached = False
            # Run analyzer (silent fallback)
            try:
                ad = analyzer.analyze(str(audio_file)).to_dict()
            except Exception as e:
                print(f"[{i}/{len(songs)}] ERROR analyzing {folder}: {e}")
                continue
        
        issues = []
        
        # 1. Check beatOffset and Tempo alignment
        tempo_game = m.get("tempo", 120)
        
        sections = ad.get("bpm_sections") or []
        if sections:
            expected_bpm = sections[0]["tempo"]
        else:
            expected_bpm = ad["final_bpm"]
            
        expected_tempo = writer.tempo_json_value(expected_bpm)
        
        start_off = float(m.get("startSongOffset") or 0.0)
        expected_offset = writer.beat_offset_ms(ad["first_downbeat_s"], expected_bpm, start_off)
        offset_game = m.get("beatOffset", 0)
        
        # Allow small +/- 3ms rounding tolerance for offset
        if abs(offset_game - expected_offset) > 3:
            issues.append(f"Sync mismatch: Meta beatOffset is {offset_game}ms, but analyzer expects {expected_offset}ms (diff: {offset_game - expected_offset}ms)")
            
        if abs(tempo_game - expected_tempo) > 0.01:
            issues.append(f"Tempo mismatch: Meta tempo is {tempo_game} BPM, but analyzer expects {expected_tempo} BPM")
            
        # 2. Check tracking confidence / grid residual
        residual = ad.get("grid_residual_ms", 0.0)
        confidence = ad.get("confidence", 1.0)
        consistency = ad.get("consistency", "CONSISTENT")
        
        if confidence < 0.6:
            issues.append(f"Low beat tracking confidence ({confidence:.2f})")
            
        if residual > 30.0:
            issues.append(f"High grid residual error ({residual:.1f} ms) - beats might drift or feel loose")
            
        if consistency == "VARIABLE":
            issues.append(f"Variable tempo detected ({len(ad.get('bpm_sections', []))} sections)")
            
        # 3. Check version variant plausibility
        # Extract title and artist from Meta.json
        song_name = m.get("songName", "")
        if " - " in song_name:
            artist, title = (p.strip() for p in song_name.split(" - ", 1))
        else:
            artist, title = "", song_name
            
        # Extract tags from OGG
        t_title, t_artist = from_tags(audio_file)
        
        # Check against folder name and tags
        if not _is_acoustid_plausible(title, artist, folder, t_title, t_artist):
            issues.append(f"Implausible version matched: Meta has '{song_name}', but folder/tags suggest a different version (e.g. live, acoustic, or wrong song)")
            
        # 4. Check Lyrics status
        lrc_key = lyrics.song_key(m)
        lrc_dir = lyrics.cache_dir()
        lrc_file = lrc_dir / f"{lrc_key}.lrc"
        miss_file = lrc_dir / f"{lrc_key}.miss"
        
        if not lrc_file.exists() and not miss_file.exists():
            issues.append("Missing lyrics file (no .lrc or .miss found in mod cache)")
        elif lrc_file.exists():
            # Read first few lines of LRC to verify by-tag and check if it's "live/acoustic" but the song is not
            try:
                lrc_text = lrc_file.read_text(encoding="utf-8", errors="replace")
                lrc_lines = lrc_text.splitlines()
                ti_tag = ""
                for line in lrc_lines[:5]:
                    if line.startswith("[ti:"):
                        ti_tag = line[4:-1]
                        break
                
                # Check for variant keywords mismatch in lyric title vs metadata title
                ac_var = _extract_keywords(ti_tag) & {"live", "remix", "acoustic", "instrumental"}
                meta_var = _extract_keywords(title) & {"live", "remix", "acoustic", "instrumental"}
                if ac_var != meta_var:
                    issues.append(f"Lyric mismatch: Lyric [ti:{ti_tag}] is a different version variant than the song title '{title}'")
            except Exception as e:
                issues.append(f"Error checking lyrics file: {e}")
                
        if issues:
            flagged.append({
                "folder": folder,
                "title": song_name,
                "issues": issues,
                "is_cached": is_cached,
                "confidence": confidence,
                "residual": residual
            })
            
    if not flagged:
        print("All songs are fully aligned, highly confident, and correctly synchronized!")
        # Clear/write empty report
        try:
            Path("C:/Users/Administrator/.gemini/antigravity-cli/brain/ac456087-aacc-47c8-9e0b-b5860f98af79/diagnostics_report.md").write_text("# dead_as_disco Sync & Lyrics Diagnostics Report\n\nAll songs are fully aligned, highly confident, and correctly synchronized!", encoding="utf-8")
        except Exception:
            pass
        return
        
    print(f"Found {len(flagged)} song(s) with potential sync, metadata, or lyric issues:\n")
    
    md = []
    md.append("# dead_as_disco Sync & Lyrics Diagnostics Report")
    md.append("")
    md.append("> [Tailored Diagnostics]")
    md.append(f"> Scanned **{len(songs)}** imported songs in the library. Found **{len(flagged)}** songs that have potential sync mismatches, low beat tracking confidence, or incorrect version/lyric variants.")
    md.append("")
    md.append("## Summary of Diagnostic Recommendations")
    md.append("1. **Automatic Sync Fixes**: For beatOffset and Tempo mismatches, run:")
    md.append("   ```powershell")
    md.append("   python -m dadtool.cli batch")
    md.append("   ```")
    md.append("2. **Version / Lyric Mismatches**: For songs flagged with implausible versions, run the corresponding ASR command to regenerate correct lyrics locally.")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## Flagged Songs")
    md.append("")

    for idx, item in enumerate(flagged, 1):
        print(f"{idx}. [{item['title']}]")
        print(f"   Folder: {item['folder']}")
        print(f"   Beat Confidence: {item['confidence']:.2f} | Grid Residual: {item['residual']:.1f} ms | Cached: {item['is_cached']}")
        print("   Issues:")
        
        md.append(f"### {idx}. {item['title']}")
        md.append(f"- **Folder**: `{item['folder']}`")
        md.append(f"- **Confidence**: `{item['confidence']:.2f}` | **Grid Residual**: `{item['residual']:.1f} ms`")
        md.append("- **Issues Found**:")
        
        for issue in item["issues"]:
            print(f"     - {issue}")
            md.append(f"  - ❌ {issue}")
            
        # Give exact recommendation on how to fix
        print("   Recommended Fix command:")
        has_sync_issue = any("Sync mismatch" in iss for iss in item["issues"])
        has_variant_issue = any("Implausible version" in iss or "Lyric mismatch" in iss for iss in item["issues"])
        
        cmds = []
        if has_variant_issue:
            cmds.append(f"python -m dadtool.cli lyrics \"{item['folder']}\" --force --reference off")
        if has_sync_issue:
            cmds.append(f"python -m dadtool.cli write \"{item['folder']}\"")
            
        if not cmds:
            cmds.append(f"python -m dadtool.cli preview \"{item['folder']}\"  (Ear-check beats)")
            
        cmd_str = ' && '.join(cmds)
        print(f"     {cmd_str}")
        print("-" * 80)
        
        md.append("- **Recommended Command to Fix**:")
        md.append(f"  ```powershell\n  {cmd_str}\n  ```")
        md.append("")
        
    # Summarize actions
    print("\nSummary of Diagnostic Recommendations:")
    print("  1. Run 'python -m dadtool.cli batch' to automatically fix all beatOffset / sync mismatches.")
    print("  2. For variant/lyric mismatches (e.g. live version matched to studio), run the recommended lyrics command to regenerate them via local ASR.")

    try:
        Path("C:/Users/Administrator/.gemini/antigravity-cli/brain/ac456087-aacc-47c8-9e0b-b5860f98af79/diagnostics_report.md").write_text("\n".join(md), encoding="utf-8", newline="\n")
    except Exception:
        pass

if __name__ == "__main__":
    main()
