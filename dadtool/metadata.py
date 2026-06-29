"""Look up a clean Song Name + Artist for an audio file, to fill the in-game menu.

Order: AcoustID audio fingerprint (primary, via MusicBrainz) -> embedded tags
(mutagen) -> cleaned filename. The fingerprint lookup yields canonical names even
for messy YouTube-rip filenames; tags and filename splitting are fallbacks.
"""
from __future__ import annotations

import re
from pathlib import Path

_JUNK = [
    r"\(Official[^)]*\)", r"\[[^\]]*\]", r"\([^)]*Lyric[^)]*\)",
    r"\([^)]*Visuali[sz]er[^)]*\)", r"\(Audio\)", r"\(HD[^)]*\)", r"\(4K[^)]*\)",
    r"\(Remastered[^)]*\)", r"Official Music Video", r"Official Video",
    r"\bHQ\b", r"\bHD\b", r"\bMV\b",
]

_COMMON_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "up", "about", "into", "over", "after", "you", "me", "him", "her",
    "it", "us", "them", "my", "your", "his", "its", "our", "their", "this", "that",
    "these", "those", "is", "am", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "shall", "should", "can", "could",
    "may", "might", "must", "official", "lyrics", "audio", "video", "remix", "version",
    "track", "unknown", "song", "music", "untitled", "export", "rip", "file", "sound",
    "mp3", "flac", "wav", "ogg", "m4a"
}


def _extract_keywords(text: str) -> set[str]:
    """Split text into lowercase alphanumeric words, filtering out common/short/generic words and digits."""
    cleaned = re.sub(r'[_.\-–—]', ' ', text)
    cleaned = re.sub(r'\d+', ' ', cleaned)
    words = re.findall(r'[a-zA-Z]+', cleaned.lower())
    return {w for w in words if len(w) > 2 and w not in _COMMON_WORDS}


def _is_acoustid_plausible(ac_title: str, ac_artist: str, filename_stem: str, tag_title: str | None = None, tag_artist: str | None = None) -> bool:
    """Return False if the AcoustID match contradicts specific keywords in the filename or tags."""
    ac_title_words = _extract_keywords(ac_title)
    ac_artist_words = _extract_keywords(ac_artist)
    
    fn_words = _extract_keywords(filename_stem)
    t_words = _extract_keywords(tag_title or "")
    ta_words = _extract_keywords(tag_artist or "")
    source_words = fn_words | t_words | ta_words

    if ac_title_words & source_words:
        return True

    if ac_artist_words & source_words:
        other_source_words = source_words - ac_artist_words
        if not other_source_words:
            return True
        return False

    return not source_words


def from_tags(path) -> tuple[str | None, str | None]:
    try:
        from mutagen import File as MF
        f = MF(str(path), easy=True)
    except Exception:  # noqa: BLE001
        return None, None
    if not f:
        return None, None

    def g(key):
        v = f.get(key)
        return v[0].strip() if v and v[0].strip() else None

    return g("title"), g("artist")


def from_acoustid(path) -> tuple[str | None, str | None]:
    """Canonical (title, artist) via AcoustID audio fingerprint -> MusicBrainz.
    Needs acoustid_api_key + fpcalc_path in dad_config.json. Returns (None, None)
    on any miss/error so callers can fall back."""
    import os

    from . import paths
    cfg = paths.load_config()
    key = cfg.get("acoustid_api_key")
    if not key:
        return None, None
    try:
        import acoustid
        if cfg.get("fpcalc_path"):
            os.environ["FPCALC"] = cfg["fpcalc_path"]
        results = list(acoustid.match(key, str(path)))
    except Exception:  # noqa: BLE001  (network/key/fingerprint failure -> fall back)
        return None, None
    if not results:
        return None, None
    top = max(s for s, *_ in results)
    if top < 0.5:  # weak fingerprint match -> don't trust it
        return None, None
    # one fingerprint can link to many MB recordings (some wrong); take the consensus
    from collections import Counter
    votes: Counter = Counter()
    for score, _rid, title, artist in results:
        if title and score >= top - 0.05:
            votes[(title.strip(), (artist or "").strip())] += 1
    if not votes:
        return None, None
    (title, artist), _ = votes.most_common(1)[0]

    # Plausibility check against filename/embedded tags to prevent false matches
    try:
        t, a = from_tags(path)
        stem = Path(path).stem
        if not _is_acoustid_plausible(title, artist or "", stem, t, a):
            return None, None
    except Exception:  # noqa: BLE001
        pass

    return title, (artist or None)


def _clean(raw: str) -> str:
    for j in _JUNK:
        raw = re.sub(j, "", raw, flags=re.I)
    return re.sub(r"\s{2,}", " ", raw).strip(" -–—_")


def _looks_like_channel(artist: str) -> bool:
    a = artist.strip().lower()
    return a.endswith("vevo") or a.endswith("- topic") or a.endswith("official") or "records" in a


def lookup(path) -> tuple[str, str]:
    """Best-effort (title, artist) for the in-game menu. Uses tag title/artist but
    ignores channel-style artists (VEVO/Topic/Official) and strips video cruft;
    falls back to splitting an 'Artist - Title' name. AcoustID (audio fingerprint)
    is tried first for canonical names; use --name/--artist to override."""
    at, aa = from_acoustid(path)
    if at:
        return at, (aa or "")
    t, a = from_tags(path)
    if a and _looks_like_channel(a):
        a = None
    raw = _clean(t or Path(path).stem)
    parts = [p.strip() for p in re.split(r"\s[-–—]\s", raw) if p.strip()]
    if a:
        title = parts[-1] if len(parts) >= 2 and a.lower() in raw.lower() else raw
        return title or raw, a
    if len(parts) == 2:
        return parts[1], parts[0]  # "Artist - Title"
    return (raw or Path(path).stem), ""


def display_label(title: str, artist: str) -> tuple[str, list[str]]:
    """The in-game labeling convention: artist FIRST in songName so the menu (which
    sorts lexicographically by songName) groups by artist, with performedBy left blank
    to avoid a redundant second line. Returns (songName, performedBy).

    Idempotent: an already-"Artist - Title" name is not prefixed again, so re-running
    relabel/rename is safe.
    """
    title = (title or "").strip()
    artist = (artist or "").strip()
    if artist and title:
        if title.lower().startswith((artist + " - ").lower()):
            name = title
        else:
            name = f"{artist} - {title}"
    else:
        name = title or artist or "Unknown"
    return name, []
