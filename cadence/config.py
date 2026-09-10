"""User settings: defaults, disk persistence, deep merge, validation.

Everything the UI can customise lives here. The file is plain JSON at
%APPDATA%/Cadence/settings.json so it can be hand-edited or synced.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

APP_NAME = "Cadence"

# Apple Music for Windows. Matched as a prefix so package version bumps
# and the "!App" activation suffix do not break detection.
APPLE_MUSIC_AUMID_PREFIX = "AppleInc.AppleMusicWin"

DEFAULTS: dict[str, Any] = {
    "source": {
        # "apple"  -> only follow Apple Music
        # "any"    -> follow whichever player Windows says is current
        # "pinned" -> follow the AUMID in source.pinned_aumid
        "mode": "apple",
        "pinned_aumid": "",
        "poll_ms": 200,
    },
    "window": {
        "layout": "card",          # card | bar | compact | art
        "always_on_top": True,
        "click_through": False,
        "frameless": True,
        "transparent": False,
        "opacity": 1.0,            # 0.25 - 1.0
        "scale": 1.0,              # 0.7 - 1.6
        "corner_radius": 18,
        "remember_position": True,
        "x": None,
        "y": None,
        "snap_to_edges": True,
        "hide_when_paused": False,
        "hide_when_fullscreen": False,
    },
    "theme": {
        "name": "midnight",        # see web/themes.js
        "adaptive": True,          # pull colours from the album art
        "adaptive_strength": 0.85,  # 0..1 blend toward artwork colours
        "accent_override": "",     # "#ff2d55" wins over everything
        "font": "system",          # system | inter | mono | serif | rounded
        "font_scale": 1.0,
        "background_art": True,    # blurred art behind the player
        "background_art_blur": 42,
        "background_art_opacity": 0.5,
        "custom_css": "",          # injected last; escape hatch for anything
    },
    "display": {
        "show_artwork": True,
        "show_progress": True,
        "show_controls": True,
        "show_meta": True,         # genre / year / album line
        "show_lyrics": True,
        "show_source_badge": True,
        "marquee_long_titles": True,
        "time_style": "elapsed",   # elapsed | remaining | both
    },
    "lyrics": {
        "enabled": True,
        "provider": "lrclib",
        "synced": True,
        "offset_ms": 0,
        "lines_visible": 5,
    },
    "enrich": {
        "enabled": True,           # iTunes Search API: hi-res art, genre, year
        "artwork_size": 1000,
        "country": "us",
        "cache_days": 30,
    },
    "history": {
        "enabled": True,
        # A play is logged once it passes either threshold (the Last.fm rule).
        "min_seconds": 60,
        "min_fraction": 0.5,
        "ignore_sources": [],
    },
    "remote": {
        # Phone remote served on the LAN. Off by default: it lets anyone
        # with the link control playback, so it is opt-in only.
        "enabled": False,
        "port": 8899,
        "token": "",
    },
    "playlists": {
        # Cadence keeps its own playlists and plays them by handing each
        # track's Apple Music link to a client. The target is explicit
        # because the itmss:// association is often owned by whichever
        # Apple client was installed last.
        "launch_target": "default",
        "autoplay_next": True,
        "confirm_before_launch": False,
    },
    "phone_audio": {
        # Act as a Bluetooth speaker for a phone, so Apple Music playing on
        # the phone comes out of the PC. The phone's AVRCP metadata then
        # shows up as an ordinary media session and the player just works.
        "enabled": False,
        "preferred_device_id": "",
        "auto_connect": False,
    },
    "hotkeys": {
        "enabled": True,
        "bindings": {
            "play_pause": "Ctrl+Alt+Space",
            "next": "Ctrl+Alt+Right",
            "previous": "Ctrl+Alt+Left",
            "toggle_window": "Ctrl+Alt+M",
            "toggle_click_through": "Ctrl+Alt+T",
            "cycle_layout": "Ctrl+Alt+L",
            "show_stats": "Ctrl+Alt+S",
        },
    },
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    return config_dir() / "settings.json"


def _deep_merge(base: dict, over: dict) -> dict:
    """Overlay `over` onto `base`, recursing into dicts only."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _clamp(v, lo, hi, fallback):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return fallback


def _validate(cfg: dict) -> dict:
    w, t = cfg["window"], cfg["theme"]
    w["opacity"] = _clamp(w.get("opacity"), 0.25, 1.0, 1.0)
    w["scale"] = _clamp(w.get("scale"), 0.7, 1.6, 1.0)
    w["corner_radius"] = int(_clamp(w.get("corner_radius"), 0, 48, 18))
    if w.get("layout") not in ("card", "bar", "compact", "art"):
        w["layout"] = "card"

    t["adaptive_strength"] = _clamp(t.get("adaptive_strength"), 0.0, 1.0, 0.85)
    t["font_scale"] = _clamp(t.get("font_scale"), 0.75, 1.5, 1.0)
    t["background_art_blur"] = int(_clamp(t.get("background_art_blur"), 0, 120, 42))
    t["background_art_opacity"] = _clamp(
        t.get("background_art_opacity"), 0.0, 1.0, 0.5
    )

    cfg["source"]["poll_ms"] = int(_clamp(cfg["source"].get("poll_ms"), 80, 2000, 200))
    if cfg["source"].get("mode") not in ("apple", "any", "pinned"):
        cfg["source"]["mode"] = "apple"

    e = cfg["enrich"]
    e["artwork_size"] = int(_clamp(e.get("artwork_size"), 100, 3000, 1000))

    h = cfg["history"]
    h["min_seconds"] = int(_clamp(h.get("min_seconds"), 5, 600, 60))
    h["min_fraction"] = _clamp(h.get("min_fraction"), 0.05, 1.0, 0.5)

    lay = cfg["lyrics"]
    lay["lines_visible"] = int(_clamp(lay.get("lines_visible"), 1, 15, 5))
    return cfg


class Settings:
    """Thread-safe settings holder. `get()` returns a snapshot copy."""

    def __init__(self, path: Path | None = None):
        self._path = path or settings_path()
        self._lock = threading.RLock()
        self._data = _validate(copy.deepcopy(DEFAULTS))
        self._listeners: list = []
        self.load()

    # ---- persistence -------------------------------------------------

    def load(self) -> dict:
        with self._lock:
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    self._data = _validate(_deep_merge(DEFAULTS, raw))
                except (json.JSONDecodeError, OSError, KeyError):
                    # Corrupt file: keep defaults, move the bad one aside.
                    try:
                        self._path.rename(self._path.with_suffix(".json.bad"))
                    except OSError:
                        pass
            return copy.deepcopy(self._data)

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self._path)

    # ---- access ------------------------------------------------------

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def section(self, name: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._data.get(name, {}))

    def update(self, patch: dict, save: bool = True) -> dict:
        """Deep-merge a partial settings dict and notify listeners."""
        with self._lock:
            self._data = _validate(_deep_merge(self._data, patch or {}))
            snapshot = copy.deepcopy(self._data)
            if save:
                self.save()
        for fn in list(self._listeners):
            try:
                fn(snapshot)
            except Exception:
                pass
        return snapshot

    def reset(self) -> dict:
        with self._lock:
            self._data = _validate(copy.deepcopy(DEFAULTS))
            self.save()
        return self.get()

    def on_change(self, fn) -> None:
        self._listeners.append(fn)
