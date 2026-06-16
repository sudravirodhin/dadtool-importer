r"""Audio -> lyrics ASR worker. Runs INSIDE the isolated lyrics venv (faster-whisper,
no torch). Invoked as a subprocess by dadtool (dadtool/lyrics.py) -- mirrors
beat_this_worker.py:

    <lyrics venv>\Scripts\python.exe scripts\lrcgen_worker.py --audio "<path>" \
        --title "<title>" --artist "<artist>" [--reference auto|off|"Artist - Title"] \
        [--model large-v3] [--models-dir DIR] [--threads N]

Prints ONE line of JSON to stdout (all model/library chatter -> stderr):
    {"language","language_prob","duration","instrumental","lines":[{"t","text"}],
     "words":[...],"transcript","reconciled","corrected","kept","dropped","segments"}

Timestamps are FILE-relative (seconds from t=0 of the decoded audio); dadtool applies
the startSongOffset timing convention. Output is a DRAFT -- ASR mishears sung lyrics.
VAD is intentionally OFF (Silero drops ~half the sung vocals on busy EDM mixes); the
hallucinations that VAD-off invites over instrumental gaps are filtered by stock-phrase
match below (no_speech_prob is unreliable on heavy backing tracks, so it is not used).
"""
import argparse
import contextlib
import difflib
import json
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# Stock ASR hallucinations Whisper invents over instrumental gaps. Compared after
# normalize() (lowercased, alnum+space). The plain transcript (.txt) keeps everything,
# so a wrongly-dropped real line is always recoverable in the human proof pass.
STOCK = {
    "thanks for watching", "thank you for watching", "thanks for watching everyone",
    "thank you for watching this video", "thank you so much for watching",
    "thank you", "thank you very much", "we ll be right back", "please subscribe",
    "like and subscribe", "subscribe", "see you next time", "see you in the next video",
    "bye", "bye bye", "goodbye", "you", "the end", "music", "applause", "outro", "intro",
    "subtitles by the amara org community", "amara org", "transcription by",
    "copyright", "all rights reserved",
}


def normalize(t):
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def is_hallucination(text):
    n = normalize(text)
    if not n:
        return True
    if n in STOCK:
        return True
    if re.fullmatch(r"[\W_]+", text.strip() or "x"):  # pure symbols / music notes
        return True
    return False


_MUSIC = re.compile(r"[\U0001F3B5\U0001F3B6\U0001F3BC♪♫♬♩]")  # 🎵🎶🎼 ♪♫♬♩


def _clean_line(t: str) -> str:
    return _MUSIC.sub("", t).strip(" -–—")


def _char_script(ch: str) -> str:
    o = ord(ch)
    if 0x3040 <= o <= 0x30ff or 0x3400 <= o <= 0x9fff or 0xac00 <= o <= 0xd7a3:
        return "cjk"
    if 0x0400 <= o <= 0x04ff:
        return "cyrillic"
    if 0x0590 <= o <= 0x06ff:
        return "semitic"
    if 0x0e00 <= o <= 0x0eff:
        return "thai_lao"
    if 0x1780 <= o <= 0x17ff:
        return "khmer"
    if ch.isascii() and ch.isalpha():
        return "latin"
    return "other"


def _line_script(text: str):
    counts: dict = {}
    for ch in text:
        if ch.isalpha():
            s = _char_script(ch)
            counts[s] = counts.get(s, 0) + 1
    return max(counts, key=counts.get) if counts else None


def ts(t):
    if not t or t < 0:
        t = 0.0
    m = int(t // 60)
    return f"{m:02d}:{t - m * 60:05.2f}"


def clean_title(title):
    t = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title)              # drop (Remix)/(Official..)/[..]
    t = re.sub(r"\s*(feat\.|ft\.|featuring)\s.*$", "", t, flags=re.I)
    return t.strip() or title.strip()


def fetch_reference(artist, title):
    """Plain reference WORDS via syncedlyrics (timing is never taken from here). The DB
    may return a different version/language than the audio -- reconcile() guards against
    that, so this just returns whatever lines it finds."""
    try:
        import syncedlyrics
        q = f"{clean_title(title)} {artist}".strip()
        log(f"[ref] syncedlyrics search: {q!r}")
        res = syncedlyrics.search(q)
    except Exception as e:  # network / parse / not found
        log(f"[ref] failed: {e}")
        return []
    if not res:
        log("[ref] no reference found")
        return []
    out = []
    for ln in res.splitlines():
        ln = re.sub(r"\[\d{1,2}:\d{2}(\.\d{1,3})?\]", "", ln)    # strip [mm:ss.xx]
        ln = re.sub(r"\[[a-z]+:[^\]]*\]", "", ln, flags=re.I)    # strip [ti:]/[ar:]/..
        ln = ln.strip()
        if ln and not re.fullmatch(r"\[[^\]]*\]", ln):           # skip [Chorus] headers
            out.append(ln)
    log(f"[ref] {len(out)} reference lines")
    return out


def fetch_synced(artist, title):
    """Try to get a SYNCED (timestamped) LRC online (syncedlyrics: LRClib/Musixmatch/NetEase/
    Megalobiz). Returns (lines, ok) with lines = [{'t': sec_in_ORIGINAL_track, 'text'}]; the
    caller (dadtool) trim-corrects via startSongOffset. ok=False -> fall back to ASR."""
    try:
        import syncedlyrics
    except Exception as e:  # not installed
        log(f"[synced] syncedlyrics unavailable: {e}")
        return [], False
    term = f"{clean_title(title)} {artist}".strip()
    if not term:
        return [], False
    log(f"[synced] syncedlyrics search: {term!r}")
    res = None
    for kw in ({"synced_only": True}, {}):
        try:
            res = syncedlyrics.search(term, **kw)
        except TypeError:
            continue  # signature without this kwarg
        except Exception:
            res = None
        if res and re.search(r"\[\d+:\d+", res):  # accept only if it actually has timestamps
            break
        res = None
    if not res:
        log("[synced] no synced lyrics found")
        return [], False
    lines = []
    for ln in res.splitlines():
        m = re.match(r"\s*((?:\[\d{1,2}:\d{2}(?:\.\d{1,3})?\])+)(.*)", ln)
        if not m:
            continue
        text = re.sub(r"<\d{1,2}:\d{2}(?:\.\d{1,3})?>", "", m.group(2)).strip()  # strip word stamps
        if not text:
            continue
        for mm, ss in re.findall(r"\[(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\]", m.group(1)):
            lines.append({"t": round(int(mm) * 60 + float(ss), 2), "text": text})
    lines.sort(key=lambda x: x["t"])
    log(f"[synced] {len(lines)} synced lines")
    return lines, len(lines) >= 4


def reconcile(lines, ref_lines):
    """Conservatively repair ASR mishearings using the closest reference line, KEEPING
    ASR timing. Accept a swap only when the lines are near-but-not-identical (0.50<=
    ratio<0.95) and share >=2 tokens -- so a wrong-language or wrong-version reference
    (no token overlap) can't corrupt good ASR. Returns (lines, n_corrected)."""
    if not ref_lines:
        return lines, 0
    ref = [(r, set(normalize(r).split())) for r in ref_lines]
    corrected = 0
    for ln in lines:
        a = normalize(ln["text"])
        atok = set(a.split())
        if len(atok) < 2:
            continue
        best, best_r = None, 0.0
        for r, rtok in ref:
            if len(atok & rtok) < 2:
                continue
            ratio = difflib.SequenceMatcher(None, a, normalize(r)).ratio()
            if ratio > best_r:
                best, best_r = r, ratio
        if best is not None and 0.50 <= best_r < 0.95:
            ln["text"] = best
            corrected += 1
    return lines, corrected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--reference", default="auto")     # auto | off | "Artist - Title" query
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--models-dir", default=None)
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--min-lines", type=int, default=4)  # fewer real lines -> instrumental
    a = ap.parse_args()

    # Online-first: if a real SYNCED LRC exists, use it (real words) and skip ASR entirely.
    # dadtool trim-corrects the timing. Falls through to ASR when nothing usable is found.
    if a.reference != "off":
        with contextlib.redirect_stdout(sys.stderr):
            synced_lines, synced_ok = fetch_synced(a.artist, a.title)
        if synced_ok:
            result = {
                "language": None, "language_prob": 0.0, "duration": 0.0, "instrumental": False,
                "lines": synced_lines, "words": [],
                "transcript": "\n".join(l["text"] for l in synced_lines),
                "reconciled": True, "corrected": 0, "kept": len(synced_lines),
                "dropped": 0, "segments": 0, "source": "online",
            }
            sys.stdout.write(json.dumps(result, ensure_ascii=False))
            return

    with contextlib.redirect_stdout(sys.stderr):  # keep C++/model chatter off real stdout
        import os

        from faster_whisper import WhisperModel
        from faster_whisper.audio import decode_audio

        audio = decode_audio(a.audio, sampling_rate=16000)   # PyAV decode, no ffmpeg CLI
        dur = len(audio) / 16000.0
        threads = a.threads or (os.cpu_count() or 8)
        log(f"[audio] {dur:.1f}s decoded; loading {a.model} (int8/cpu, {threads} threads)")
        model = WhisperModel(a.model, device="cpu", compute_type="int8",
                             download_root=a.models_dir, cpu_threads=threads)
        segments, info = model.transcribe(
            audio, language=None, beam_size=5, vad_filter=False,
            word_timestamps=True, condition_on_previous_text=False,
        )
        segs = list(segments)   # generator -> list (transcription happens here)
        log(f"[asr] language={info.language} p={info.language_probability:.2f}, {len(segs)} segments")

        lines, words, transcript, dropped = [], [], [], 0
        for s in segs:
            txt = _clean_line((s.text or "").strip())
            if not txt:
                continue
            transcript.append(txt)
            if is_hallucination(txt):
                dropped += 1
                continue
            lines.append({"t": round(float(s.start), 2), "text": txt})
            for w in (s.words or []):
                ww = (w.word or "").strip()
                if ww:
                    words.append({"start": round(float(w.start), 3), "end": round(float(w.end), 3),
                                  "word": ww, "prob": round(float(w.probability), 3)})

        # Drop cross-language hallucinations (e.g. a stray Khmer line in an English song):
        # keep only lines in the song's DOMINANT script.
        scr = [x for x in (_line_script(l["text"]) for l in lines) if x]
        if scr:
            dom = max(set(scr), key=scr.count)
            before = len(lines)
            lines = [l for l in lines if (_line_script(l["text"]) or dom) == dom]
            dropped += before - len(lines)

        instrumental = len(lines) < a.min_lines
        corrected, reconciled = 0, False
        if not instrumental and a.reference != "off":
            if a.reference == "auto":
                ref = fetch_reference(a.artist, a.title)
            elif " - " in a.reference:
                ar, ti = a.reference.split(" - ", 1)
                ref = fetch_reference(ar, ti)
            else:
                ref = fetch_reference(a.artist, a.reference)
            lines, corrected = reconcile(lines, ref)
            reconciled = bool(ref)

        result = {
            "language": info.language, "language_prob": round(float(info.language_probability), 3),
            "duration": round(dur, 2), "instrumental": instrumental,
            "lines": lines, "words": words, "transcript": "\n".join(transcript),
            "reconciled": reconciled, "corrected": corrected,
            "kept": len(lines), "dropped": dropped, "segments": len(segs),
            "source": "asr",
        }
        log(f"[asr] kept {len(lines)} lines, dropped {dropped} hallucination(s), "
            f"corrected {corrected}; instrumental={instrumental}")

    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("FATAL:\n" + traceback.format_exc())
        sys.exit(1)
