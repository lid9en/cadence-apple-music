"""Application controller and the JavaScript bridge.

Owns the media engine, the history database, the hotkey manager and the
WebView window, and exposes a small RPC surface to the UI as
`window.pywebview.api.*`.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import (applemusic_fix, enrich, lyrics as lyrics_mod, palette,
               phone_audio, remote, win_effects)
from .config import Settings
from .history import History, PlayTracker
from .hotkeys import HotkeyManager
from .media import MediaEngine

def _web_dir() -> Path:
    """Locate the bundled UI, both from source and inside a frozen build.

    PyInstaller extracts data files to sys._MEIPASS, and a module's
    __file__ inside the archive is not always an absolute path, so the
    source-relative guess has to come last.
    """
    import sys

    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates += [Path(meipass) / "cadence" / "web", Path(meipass) / "web"]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "cadence" / "web")
    candidates.append(Path(__file__).resolve().parent / "web")

    for c in candidates:
        try:
            if (c / "index.html").is_file():
                return c
        except OSError:
            continue
    return candidates[-1]


WEB_DIR = _web_dir()

log = logging.getLogger(__name__)

LAYOUT_SIZES = {
    "card": (380, 560),
    "bar": (680, 104),
    "compact": (400, 148),
    "art": (330, 330),
}
PANEL_SIZE = (980, 700)


def _data_uri(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class CadenceApp:
    def __init__(self):
        self.settings = Settings()
        self.history = History()
        self.tracker = PlayTracker(self.history, self.settings)
        self.engine = MediaEngine(self.settings)
        self.hotkeys = HotkeyManager(self._on_hotkey)
        self.phone = phone_audio.PhoneAudioBridge(self.settings)
        self.remote = remote.RemoteServer(self.settings,
                                          _RemoteFacade(self))

        self.window = None
        self.view = "player"
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="cadence-bg")
        self._lock = threading.RLock()

        # Per-track derived data, only valid for _current_key.
        self._current_key = ""
        self._meta: dict = {}
        self._lyrics: dict = {"found": False, "synced": False, "lines": []}
        self._palette: dict = dict(palette.DEFAULT)
        self._art_uri: str = ""
        self._art_token: str = ""
        self._hidden = False

        self.engine.on_track_change = self._on_track_change
        self.engine.on_tick = self._on_tick
        self.settings.on_change(self._on_settings_change)

    # ---- lifecycle -----------------------------------------------------

    def start_backend(self) -> None:
        self.engine.start()
        self.phone.start()
        if self.settings.section("remote").get("enabled"):
            self.remote.start()
        cfg = self.settings.get()
        self.hotkeys.start()
        self.hotkeys.apply(
            cfg["hotkeys"]["bindings"], cfg["hotkeys"].get("enabled", True)
        )

    def shutdown(self) -> None:
        try:
            self.tracker._flush()
        except Exception:
            pass
        self.remote.stop()
        self.engine.stop()
        self.phone.stop()
        self.hotkeys.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.history.close()

    # ---- engine callbacks ----------------------------------------------

    def _on_tick(self, st: dict) -> None:
        self.tracker.tick(st)

    def _on_track_change(self, st: dict) -> None:
        key = st.get("track_key") or ""
        with self._lock:
            self._current_key = key
            self._meta = {}
            self._lyrics = {"found": False, "synced": False, "lines": []}
            art, token = self.engine.artwork()
            self._art_token = token
            self._art_uri = _data_uri(art, _sniff_mime(art)) if art else ""
            self._palette = palette.from_artwork(art)
        self._pool.submit(self._enrich_track, key, dict(st))

    def _enrich_track(self, key: str, st: dict) -> None:
        """Background: iTunes metadata, hi-res art, lyrics. Cached in SQLite."""
        cfg = self.settings.get()

        if cfg["enrich"].get("enabled", True):
            meta = None
            cached = self.history.cache_get(
                "meta_cache", key, int(cfg["enrich"].get("cache_days", 30))
            )
            if cached:
                try:
                    meta = json.loads(cached)
                except json.JSONDecodeError:
                    meta = None
            if meta is None:
                meta = enrich.lookup(
                    st.get("title", ""), st.get("artist", ""), st.get("album", ""),
                    country=cfg["enrich"].get("country", "us"),
                    artwork_size=int(cfg["enrich"].get("artwork_size", 1000)),
                )
                self.history.cache_put("meta_cache", key, json.dumps(meta))

            if self._current_key != key:
                return
            with self._lock:
                self._meta = meta or {}

            # Upgrade to the high-resolution sleeve when one exists.
            url = (meta or {}).get("artwork_url")
            if url:
                data = enrich.fetch_bytes(url)
                if data and self._current_key == key:
                    with self._lock:
                        self._art_uri = _data_uri(data, _sniff_mime(data))
                        self._art_token = f"hi:{key}"
                        self._palette = palette.from_artwork(data)

        if cfg["lyrics"].get("enabled", True):
            ly = None
            cached = self.history.cache_get("lyrics_cache", key, 90)
            if cached:
                try:
                    ly = json.loads(cached)
                except json.JSONDecodeError:
                    ly = None
            if ly is None:
                ly = lyrics_mod.fetch(
                    st.get("title", ""), st.get("artist", ""),
                    st.get("album", ""), st.get("duration", 0),
                )
                self.history.cache_put("lyrics_cache", key, json.dumps(ly))
            if self._current_key == key:
                with self._lock:
                    self._lyrics = ly

    def _on_settings_change(self, cfg: dict) -> None:
        self.hotkeys.apply(
            cfg["hotkeys"]["bindings"], cfg["hotkeys"].get("enabled", True)
        )
        if self.window:
            try:
                win_effects.apply_all(self.window, cfg["window"])
            except Exception:
                pass

    # ---- hotkeys --------------------------------------------------------

    def _on_hotkey(self, action: str) -> None:
        if action == "play_pause":
            self.engine.play_pause()
        elif action == "next":
            self.engine.next()
        elif action == "previous":
            self.engine.previous()
        elif action == "toggle_window":
            self.toggle_window()
        elif action == "toggle_click_through":
            cur = self.settings.section("window").get("click_through", False)
            self.settings.update({"window": {"click_through": not cur}})
        elif action == "cycle_layout":
            order = ["card", "bar", "compact", "art"]
            cur = self.settings.section("window").get("layout", "card")
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "card"
            self.set_layout(nxt)
        elif action == "show_stats":
            self.api_set_view("stats" if self.view != "stats" else "player")

    def toggle_window(self) -> None:
        if not self.window:
            return
        try:
            if self._hidden:
                self.window.show()
            else:
                self.window.hide()
            self._hidden = not self._hidden
        except Exception:
            pass

    def set_layout(self, layout: str) -> None:
        self.settings.update({"window": {"layout": layout}})
        if self.view == "player":
            self._resize_for_view()

    def _resize_for_view(self) -> None:
        if not self.window:
            return
        if self.view == "player":
            layout = self.settings.section("window").get("layout", "card")
            w, h = LAYOUT_SIZES.get(layout, LAYOUT_SIZES["card"])
            scale = float(self.settings.section("window").get("scale", 1.0))
            w, h = int(w * scale), int(h * scale)
        else:
            w, h = PANEL_SIZE
        try:
            self.window.resize(w, h)
        except Exception:
            pass

    # =====================================================================
    # RPC surface -- everything below is callable from JavaScript as
    # window.pywebview.api.<name>(...)
    # =====================================================================

    def get_state(self) -> dict[str, Any]:
        st = self.engine.state()
        with self._lock:
            st["meta"] = dict(self._meta)
            st["palette"] = dict(self._palette)
            st["art_token"] = self._art_token
            ly = self._lyrics
        cfg = self.settings.get()
        if cfg["lyrics"].get("enabled", True) and ly.get("found"):
            idx = lyrics_mod.active_index(
                ly.get("lines", []), st.get("position", 0),
                int(cfg["lyrics"].get("offset_ms", 0)),
            )
            st["lyrics"] = {
                "found": True,
                "synced": ly.get("synced", False),
                "active": idx,
                "lines": ly.get("lines", []),
            }
        else:
            st["lyrics"] = {"found": False, "synced": False, "active": -1, "lines": []}
        st["view"] = self.view
        return st

    def get_artwork(self) -> dict[str, str]:
        with self._lock:
            return {"token": self._art_token, "uri": self._art_uri}

    def get_settings(self) -> dict:
        return self.settings.get()

    def update_settings(self, patch: dict) -> dict:
        cfg = self.settings.update(patch or {})
        if isinstance(patch, dict) and "window" in patch:
            w = patch["window"]
            if "layout" in w or "scale" in w:
                self._resize_for_view()
        return cfg

    def reset_settings(self) -> dict:
        cfg = self.settings.reset()
        self._on_settings_change(cfg)
        self._resize_for_view()
        return cfg

    def get_hotkey_status(self) -> dict:
        return {"failures": dict(self.hotkeys.failures)}

    def control(self, action: str, value: Any = None) -> dict:
        ok = False
        if action == "play_pause":
            ok = self.engine.play_pause()
        elif action == "play":
            ok = self.engine.play()
        elif action == "pause":
            ok = self.engine.pause()
        elif action == "next":
            ok = self.engine.next()
        elif action == "previous":
            ok = self.engine.previous()
        elif action == "seek":
            ok = self.engine.seek(float(value or 0))
        elif action == "shuffle":
            ok = self.engine.set_shuffle(bool(value))
        elif action == "repeat":
            ok = self.engine.set_repeat(str(value or "none"))
        return {"ok": ok, "action": action}

    def open_external(self, url: str) -> dict:
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            webbrowser.open(url)
            return {"ok": True}
        return {"ok": False}

    def get_stats(self, days: int = 0) -> dict:
        return self.history.stats(int(days or 0))

    def clear_history(self) -> dict:
        self.history.purge()
        return {"ok": True}

    def set_view(self, name: str) -> dict:
        return self.api_set_view(name)

    def api_set_view(self, name: str) -> dict:
        log.info("set_view(%s)", name)
        if name not in ("player", "stats", "settings", "fix"):
            name = "player"
        self.view = name
        self._resize_for_view()
        return {"view": name}

    # ---- window chrome ---------------------------------------------------

    def window_action(self, action: str) -> dict:
        log.info("window_action(%s)", action)
        if not self.window:
            return {"ok": False}
        try:
            if action == "minimize":
                self.window.minimize()
            elif action == "close":
                self.window.destroy()
            elif action == "hide":
                self.toggle_window()
            return {"ok": True}
        except Exception:
            return {"ok": False}

    # ---- Apple Music repair ---------------------------------------------

    def fix_scan(self) -> dict:
        """Read-only inspection of the instant-skip problem."""
        report = applemusic_fix.scan()
        report["skip_rate"] = self.history.recent_skip_rate()
        return report

    def fix_logs(self) -> dict:
        """Decode Apple Music's own traces and report the real error codes."""
        return applemusic_fix.analyze_logs()

    def fix_watch(self, seconds: int = 20) -> dict:
        """Watch media-key traffic and track changes; returns a verdict."""
        return applemusic_fix.watch(int(seconds), self.engine)

    def fix_apply(self, remedy: str, confirm: bool = False) -> dict:
        if not confirm:
            return {"ok": False, "error": "confirmation required"}
        # The UI dialog is the confirmation; pass it through to the guard.
        return applemusic_fix.apply(remedy, destructive_ok=True)

    # ---- phone remote ----------------------------------------------------

    def remote_status(self) -> dict:
        return self.remote.status()

    def remote_start(self) -> dict:
        return self.remote.start()

    def remote_stop(self) -> dict:
        return self.remote.stop()

    # ---- phone as a Bluetooth audio source ------------------------------

    def phone_devices(self) -> dict:
        """Paired devices that can send audio to this PC."""
        return {"devices": self.phone.list_devices(),
                "status": self.phone.status()}

    def phone_connect(self, device_id: str, name: str = "") -> dict:
        result = self.phone.connect(device_id, name)
        if result.get("ok"):
            self.settings.update(
                {"phone_audio": {"preferred_device_id": device_id}})
        return result

    def phone_arm(self, device_id: str, name: str = "",
                  seconds: int = 90) -> dict:
        """Wait for the phone to bring the audio link up."""
        self.settings.update({"phone_audio": {"preferred_device_id": device_id}})
        return self.phone.arm(device_id, name, int(seconds))

    def phone_cancel_arm(self) -> dict:
        return self.phone.cancel_arm()

    def phone_disconnect(self) -> dict:
        self.phone.cancel_arm()
        return self.phone.disconnect()

    def phone_status(self) -> dict:
        return self.phone.status()

    def open_settings_folder(self) -> dict:
        from .config import config_dir
        try:
            import os
            os.startfile(str(config_dir()))  # noqa: S606 - opens Explorer
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class _RemoteFacade:
    """The narrow surface the LAN remote is allowed to reach.

    Deliberately not the full Api: a request arriving over the network can
    read now-playing and drive transport, and nothing else. No settings, no
    repair actions, no filesystem.
    """

    def __init__(self, app: "CadenceApp"):
        self._app = app

    def state(self) -> dict:
        st = self._app.get_state()
        return {
            "connected": st.get("connected"), "playing": st.get("playing"),
            "status": st.get("status"), "title": st.get("title"),
            "artist": st.get("artist"), "album": st.get("album"),
            "position": st.get("position"), "duration": st.get("duration"),
            "can_next": st.get("can_next"), "can_prev": st.get("can_prev"),
            "can_seek": st.get("can_seek"), "shuffle": st.get("shuffle"),
            "repeat": st.get("repeat"), "source_label": st.get("source_label"),
            "art_token": st.get("art_token"), "palette": st.get("palette"),
        }

    def artwork(self) -> tuple[bytes | None, str]:
        with self._app._lock:
            uri = self._app._art_uri
        if not uri.startswith("data:"):
            return None, "application/octet-stream"
        header, _, b64 = uri.partition(",")
        mime = header[5:].split(";")[0] or "image/jpeg"
        try:
            return base64.b64decode(b64), mime
        except Exception:
            return None, "application/octet-stream"

    def playlists(self) -> dict:
        return {"playlists": []}

    def control(self, action: str, value=None) -> dict:
        allowed = {"play_pause", "play", "pause", "next", "previous",
                   "seek", "shuffle", "repeat"}
        if action not in allowed:
            return {"ok": False, "error": "not permitted"}
        return self._app.control(action, value)


class Api:
    """Exactly the methods the UI may call. Keeps lifecycle off the bridge."""

    _EXPOSED = (
        "get_state", "get_artwork", "get_settings", "update_settings",
        "reset_settings", "get_hotkey_status", "control", "open_external",
        "get_stats", "clear_history", "set_view", "window_action",
        "fix_scan", "fix_logs", "fix_watch", "fix_apply",
        "remote_status", "remote_start", "remote_stop",
        "phone_devices", "phone_connect", "phone_arm",
        "phone_cancel_arm", "phone_disconnect",
        "phone_status",
        "open_settings_folder",
    )

    def __init__(self, app: CadenceApp):
        self._app = app
        for name in self._EXPOSED:
            setattr(self, name, getattr(app, name))
