"""Extra track metadata from Apple's public iTunes Search endpoint.

SMTC hands us a title, an artist and a small thumbnail. The iTunes Search
API fills in the rest -- high-resolution artwork, genre, release year and
a link to the track on Apple Music. It is public and unauthenticated, so
this works without a developer account or a login.

Results are cached in SQLite so a repeated track costs nothing.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

SEARCH_URL = "https://itunes.apple.com/search"
UA = "Cadence/1.0 (+https://github.com/) Python-urllib"


def _http_json(url: str, timeout: float = 6.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def upscale_artwork(url: str, size: int) -> str:
    """iTunes artwork URLs end in e.g. /100x100bb.jpg; ask for bigger."""
    if not url:
        return ""
    return re.sub(r"/\d+x\d+(bb)?\.(jpg|png)$", f"/{size}x{size}bb.jpg", url)


def _clean(s: str) -> str:
    """Strip the noise that stops a search from matching."""
    s = re.sub(r"\s*[\(\[][^)\]]*(remaster|deluxe|version|edit|mix|live|feat"
               r"|explicit|bonus|mono|stereo)[^)\]]*[\)\]]", "", s, flags=re.I)
    s = re.sub(r"\s*-\s*(single|ep|remaster(ed)?( \d{4})?)$", "", s, flags=re.I)
    return s.strip()


def _score(item: dict, title: str, artist: str) -> int:
    t = (item.get("trackName") or "").lower()
    a = (item.get("artistName") or "").lower()
    want_t, want_a = title.lower(), artist.lower()
    score = 0
    if t == want_t:
        score += 5
    elif want_t in t or t in want_t:
        score += 2
    if a == want_a:
        score += 5
    elif want_a in a or a in want_a:
        score += 2
    return score


def lookup(title: str, artist: str, album: str = "", *,
           country: str = "us", artwork_size: int = 1000) -> dict:
    """Best-effort metadata. Returns {} when nothing convincing is found."""
    title, artist = (title or "").strip(), (artist or "").strip()
    if not title:
        return {}

    attempts = [f"{artist} {title}".strip()]
    ct, ca = _clean(title), _clean(artist)
    if (ct, ca) != (title, artist):
        attempts.append(f"{ca} {ct}".strip())
    if album:
        attempts.append(f"{artist} {album} {title}".strip())

    best, best_score = None, 0
    for term in attempts:
        params = urllib.parse.urlencode(
            {"term": term, "entity": "song", "limit": 8, "country": country}
        )
        try:
            data = _http_json(f"{SEARCH_URL}?{params}")
        except Exception:
            continue
        for item in data.get("results", []):
            s = _score(item, ct or title, ca or artist)
            if s > best_score:
                best, best_score = item, s
        if best_score >= 10:
            break

    if not best or best_score < 4:
        return {}

    release = best.get("releaseDate") or ""
    return {
        "matched": True,
        "score": best_score,
        "title": best.get("trackName") or "",
        "artist": best.get("artistName") or "",
        "album": best.get("collectionName") or "",
        "genre": best.get("primaryGenreName") or "",
        "year": release[:4],
        "release_date": release[:10],
        "artwork_url": upscale_artwork(best.get("artworkUrl100") or "", artwork_size),
        "apple_url": best.get("trackViewUrl") or "",
        "duration": round((best.get("trackTimeMillis") or 0) / 1000),
        "explicit": best.get("trackExplicitness") == "explicit",
    }


def _as_track(item: dict, artwork_size: int) -> dict:
    """Normalise one iTunes Search result into a Cadence track record."""
    release = item.get("releaseDate") or ""
    return {
        "title": item.get("trackName") or "",
        "artist": item.get("artistName") or "",
        "album": item.get("collectionName") or "",
        "genre": item.get("primaryGenreName") or "",
        "year": release[:4],
        "artwork_url": upscale_artwork(item.get("artworkUrl100") or "", artwork_size),
        "apple_url": item.get("trackViewUrl") or "",
        "duration": round((item.get("trackTimeMillis") or 0) / 1000),
        "explicit": item.get("trackExplicitness") == "explicit",
        "store_id": item.get("trackId"),
    }


def search(term: str, *, limit: int = 25, country: str = "us",
           artwork_size: int = 300) -> list[dict]:
    """Free-text catalogue search, for adding tracks to a playlist.

    Returns candidates in Apple's own relevance order -- unlike `lookup`,
    which scores results against a known title/artist. Only entries with a
    usable `apple_url` are kept, because a track Cadence cannot hand off
    to the Apple Music app is not worth offering.
    """
    term = (term or "").strip()
    if not term:
        return []
    params = urllib.parse.urlencode({
        "term": term, "entity": "song",
        "limit": max(1, min(50, int(limit))), "country": country,
    })
    try:
        data = _http_json(f"{SEARCH_URL}?{params}")
    except Exception:
        return []
    out = []
    for item in data.get("results", []):
        track = _as_track(item, artwork_size)
        if track["apple_url"] and track["title"]:
            out.append(track)
    return out


def fetch_bytes(url: str, timeout: float = 8.0, cap: int = 12 * 1024 * 1024):
    """Download artwork. Returns None rather than raising."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(cap + 1)
        return data if 0 < len(data) <= cap else None
    except Exception:
        return None
