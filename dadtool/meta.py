"""Read (and later write) the per-song ``Meta.json`` sidecar.

The game writes Meta.json as UTF-8 for ASCII-only names but UTF-16LE (with BOM)
when the name/path contains non-ASCII characters. We sniff the BOM, decode
accordingly, and remember the encoding so a future rewrite can preserve it
byte-for-byte in spirit (same encoding + BOM).
"""
from __future__ import annotations

import json
from pathlib import Path


def detect_encoding(raw: bytes) -> str:
    """Return a normalized encoding tag based on the byte-order mark."""
    if raw[:2] == b"\xff\xfe":
        return "utf-16-le"
    if raw[:2] == b"\xfe\xff":
        return "utf-16-be"
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    return "utf-8"


def read_meta(path) -> tuple[dict, str]:
    """Return (parsed_json, encoding_tag)."""
    raw = Path(path).read_bytes()
    enc = detect_encoding(raw)
    if enc in ("utf-16-le", "utf-16-be"):
        text = raw.decode("utf-16")  # BOM drives endianness
    else:
        text = raw.decode(enc)
    return json.loads(text), enc


def dumps_game_style(data: dict) -> str:
    """Serialize like the game does: tab indent, ``": "`` separators, CRLF."""
    return json.dumps(data, indent="\t", ensure_ascii=False).replace("\n", "\r\n")


def write_meta(path, data: dict, encoding: str) -> None:
    """Write Meta.json in the game's style, choosing the encoding by CONTENT to match
    the game: any non-ASCII anywhere in the file -> UTF-16 + BOM; pure ASCII -> UTF-8.

    The ``encoding`` arg only picks endianness/BOM flavor within those (preserve UTF-16-BE
    or the UTF-8 BOM if that's what the file had). This prevents a non-ASCII name from
    being written as a no-BOM UTF-8 file, which the game can misread as the ANSI codepage.
    Bytes are written directly so CRLF and BOM are exactly controlled.
    """
    text = dumps_game_style(data)
    p = Path(path)
    if any(ord(c) > 127 for c in text):  # game convention: non-ASCII => UTF-16 + BOM
        if encoding == "utf-16-be":
            p.write_bytes(b"\xfe\xff" + text.encode("utf-16-be"))
        else:
            p.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    elif encoding == "utf-8-sig":
        p.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    else:  # pure ASCII -> UTF-8
        p.write_bytes(text.encode("utf-8"))
