"""Lyrics from LRCLIB -- free, public, no API key, no account.

Prefers time-synced LRC so the player can highlight the current line.
Falls back to plain text when no synced version exists.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

BASE = "https://lrclib.net/api"
UA = "Cadence/1.0 (Apple Music companion for Windows)"

LRC_LINE = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def _http_json(url: str, timeout: float = 6.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def parse_lrc(lrc: str) -> list[dict]:
    """LRC text -> [{t: seconds, text: str}], sorted, blanks preserved."""
    out: list[dict] = []
    for raw in (lrc or "").splitlines():
        stamps = list(LRC_LINE.finditer(raw))
        if not stamps:
            continue
        text = LRC_LINE.sub("", raw).strip()
        for m in stamps:
            mins = int(m.group(1))
            secs = float(m.group(2).replace(":", "."))
            out.append({"t": round(mins * 60 + secs, 2), "text": text})
    out.sort(key=lambda x: x["t"])
    return out


def fetch(title: str, artist: str, album: str = "", duration: float = 0) -> dict:
    """Returns {found, synced, lines[], plain} -- never raises."""
    title, artist = (title or "").strip(), (artist or "").strip()
    if not title or not artist:
        return {"found": False, "synced": False, "lines": [], "plain": ""}

    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration and duration > 0:
        params["duration"] = int(round(duration))

    record = None
    try:
        record = _http_json(f"{BASE}/get?{urllib.parse.urlencode(params)}")
    except Exception:
        # /get demands a near-exact match; /search is fuzzier.
        try:
            q = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
            results = _http_json(f"{BASE}/search?{q}")
            if isinstance(results, list) and results:
                record = results[0]
        except Exception:
            record = None

    if not isinstance(record, dict):
        return {"found": False, "synced": False, "lines": [], "plain": ""}

    synced = record.get("syncedLyrics") or ""
    plain = record.get("plainLyrics") or ""

    if synced:
        lines = parse_lrc(synced)
        if lines:
            return {"found": True, "synced": True, "lines": lines, "plain": plain}

    if plain:
        lines = [{"t": None, "text": ln} for ln in plain.splitlines()]
        return {"found": True, "synced": False, "lines": lines, "plain": plain}

    return {"found": False, "synced": False, "lines": [], "plain": ""}


def active_index(lines: list[dict], position: float, offset_ms: int = 0) -> int:
    """Index of the line that should be highlighted, or -1."""
    if not lines or lines[0].get("t") is None:
        return -1
    pos = position + offset_ms / 1000.0
    lo, hi, found = 0, len(lines) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if lines[mid]["t"] <= pos:
            found, lo = mid, mid + 1
        else:
            hi = mid - 1
    return found
