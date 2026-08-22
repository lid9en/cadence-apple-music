"""Bridge to the Windows System Media Transport Controls (SMTC).

This is how the app talks to Apple Music for Windows. Apple ships no
plugin or extension API, but the Apple Music app publishes a media
session to Windows like every other player, and that session is both
readable (title/artist/album/artwork/position) and writable
(play/pause/next/previous/seek/shuffle/repeat).

Everything WinRT happens on one dedicated thread with its own asyncio
loop and a multi-threaded COM apartment. Callers on the UI thread use
the plain sync methods, which marshal onto that loop.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import threading
import time
from typing import Any, Callable

from .config import APPLE_MUSIC_AUMID_PREFIX

PLAYBACK_STATUS = {
    0: "Closed",
    1: "Opened",
    2: "Changing",
    3: "Stopped",
    4: "Playing",
    5: "Paused",
}

REPEAT_MODE = {0: "none", 1: "track", 2: "list"}

# Friendly names for the players we are likely to meet.
KNOWN_SOURCES = {
    "AppleInc.AppleMusicWin": "Apple Music",
    "Spotify.exe": "Spotify",
    "Chrome": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "iTunes.exe": "iTunes",
    "foobar2000.exe": "foobar2000",
    "vlc.exe": "VLC",
}


def friendly_source(aumid: str) -> str:
    if not aumid:
        return "Nothing"
    for key, label in KNOWN_SOURCES.items():
        if aumid.startswith(key):
            return label
    return aumid.split("!")[0].removesuffix(".exe")


def _td_seconds(v) -> float:
    """WinRT TimeSpan arrives as datetime.timedelta; be forgiving."""
    if v is None:
        return 0.0
    if isinstance(v, _dt.timedelta):
        return v.total_seconds()
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _age_seconds(when) -> float:
    """Seconds since a WinRT DateTime. 0 if absent, bogus or in the future."""
    if not isinstance(when, _dt.datetime):
        return 0.0
    try:
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        if when.year < 1700:  # unset DateTime shows up as the 1601 epoch
            return 0.0
        age = (_dt.datetime.now(_dt.timezone.utc) - when).total_seconds()
    except (OverflowError, ValueError, OSError):
        return 0.0
    # A wildly large age means the player never updates it; don't trust it.
    return age if 0.0 <= age < 3600.0 else 0.0


def _unwrap(v, default=None):
    """WinRT nullables (IReference<T>) surface either the value or None."""
    if v is None:
        return default
    return getattr(v, "value", v)


def track_key(title: str, artist: str, album: str) -> str:
    raw = f"{(artist or '').strip().lower()}␟{(title or '').strip().lower()}"
    raw += f"␟{(album or '').strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class MediaEngine:
    """Polls SMTC and exposes a single current-track state dict."""

    def __init__(self, settings):
        self.settings = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()

        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._empty_state()
        self._thumb: bytes | None = None
        self._thumb_token: str = ""

        self.on_track_change: Callable[[dict], None] | None = None
        self.on_tick: Callable[[dict], None] | None = None
        self._last_key: str | None = None

    # ---- lifecycle ---------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="cadence-smtc", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=10)

    def stop(self) -> None:
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread:
            self._thread.join(timeout=3)

    def _thread_main(self) -> None:
        from winrt.runtime import ApartmentType, init_apartment

        init_apartment(ApartmentType.MULTI_THREADED)
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            try:
                self._loop.close()
            finally:
                self._loop = None

    # ---- state -------------------------------------------------------

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "connected": False,
            "source": "",
            "source_label": "Nothing",
            "is_apple": False,
            "title": "",
            "artist": "",
            "album": "",
            "status": "Stopped",
            "playing": False,
            "position": 0.0,
            "duration": 0.0,
            "shuffle": False,
            "repeat": "none",
            "can_next": False,
            "can_prev": False,
            "can_seek": False,
            "track_key": "",
            "art_token": "",
            "available_sources": [],
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            s = dict(self._state)
        # Interpolate the scrubber between SMTC updates so it moves smoothly.
        if s.get("playing") and s.get("_pos_at"):
            drift = time.monotonic() - s["_pos_at"]
            s["position"] = min(s["position"] + drift, s["duration"] or 1e9)
        s.pop("_pos_at", None)
        return s

    def artwork(self) -> tuple[bytes | None, str]:
        with self._lock:
            return self._thumb, self._thumb_token

    # ---- session selection -------------------------------------------

    def _pick(self, sessions, current):
        cfg = self.settings.section("source")
        mode = cfg.get("mode", "apple")

        if mode == "apple":
            for s in sessions:
                if s.source_app_user_model_id.startswith(APPLE_MUSIC_AUMID_PREFIX):
                    return s
            return None

        if mode == "pinned":
            want = cfg.get("pinned_aumid", "")
            for s in sessions:
                if want and s.source_app_user_model_id.startswith(want):
                    return s
            return None

        # "any": prefer something actually playing, else whatever is current.
        playing = [
            s for s in sessions if int(s.get_playback_info().playback_status) == 4
        ]
        if playing:
            return playing[0]
        return current or (sessions[0] if sessions else None)

    # ---- main loop ---------------------------------------------------

    async def _run(self) -> None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Mgr,
        )

        mgr = await Mgr.request_async()
        self._session = None
        self._ready.set()

        while not self._stop.is_set():
            poll = self.settings.section("source").get("poll_ms", 200) / 1000.0
            try:
                await self._poll_once(mgr)
            except Exception as e:  # a session can vanish mid-read
                with self._lock:
                    self._state["error"] = repr(e)
            await asyncio.sleep(poll)

    async def _poll_once(self, mgr) -> None:
        sessions = list(mgr.get_sessions())
        try:
            current = mgr.get_current_session()
        except Exception:
            current = None

        available = [
            {
                "aumid": s.source_app_user_model_id,
                "label": friendly_source(s.source_app_user_model_id),
                "status": PLAYBACK_STATUS.get(
                    int(s.get_playback_info().playback_status), "?"
                ),
            }
            for s in sessions
        ]

        sess = self._pick(sessions, current)
        if sess is None:
            with self._lock:
                blank = self._empty_state()
                blank["available_sources"] = available
                self._state = blank
                self._thumb, self._thumb_token = None, ""
            self._last_key = None
            return

        self._session = sess
        aumid = sess.source_app_user_model_id
        pb = sess.get_playback_info()
        status = PLAYBACK_STATUS.get(int(pb.playback_status), "?")

        try:
            props = await sess.try_get_media_properties_async()
            title = props.title or ""
            artist = props.artist or ""
            album = props.album_title or ""
            thumb_ref = props.thumbnail
        except Exception:
            title = artist = album = ""
            thumb_ref = None

        try:
            tl = sess.get_timeline_properties()
            position = _td_seconds(tl.position)
            duration = max(0.0, _td_seconds(tl.end_time) - _td_seconds(tl.start_time))
            # Players refresh the timeline lazily -- Chrome can sit on the
            # same value for many seconds. `last_updated_time` says when the
            # reported position was true, so add the elapsed time since.
            if status == "Playing":
                position += _age_seconds(getattr(tl, "last_updated_time", None))
            if duration:
                position = max(0.0, min(position, duration))
        except Exception:
            position, duration = 0.0, 0.0

        controls = pb.controls
        key = track_key(title, artist, album) if (title or artist) else ""

        new = {
            "connected": True,
            "source": aumid,
            "source_label": friendly_source(aumid),
            "is_apple": aumid.startswith(APPLE_MUSIC_AUMID_PREFIX),
            "title": title,
            "artist": artist,
            "album": album,
            "status": status,
            "playing": status == "Playing",
            "position": position,
            "duration": duration,
            "shuffle": bool(_unwrap(pb.is_shuffle_active, False)),
            "repeat": REPEAT_MODE.get(int(_unwrap(pb.auto_repeat_mode, 0) or 0), "none"),
            "can_next": bool(getattr(controls, "is_next_enabled", False)),
            "can_prev": bool(getattr(controls, "is_previous_enabled", False)),
            "can_seek": bool(
                getattr(controls, "is_playback_position_enabled", False)
            ),
            "track_key": key,
            "available_sources": available,
            "_pos_at": time.monotonic(),
        }

        changed = key and key != self._last_key
        if changed:
            self._last_key = key
            self._thumb, self._thumb_token = await self._read_thumb(thumb_ref)

        with self._lock:
            new["art_token"] = self._thumb_token
            self._state = new

        snapshot = self.state()
        if changed and self.on_track_change:
            try:
                self.on_track_change(snapshot)
            except Exception:
                pass
        if self.on_tick:
            try:
                self.on_tick(snapshot)
            except Exception:
                pass

    async def _read_thumb(self, ref) -> tuple[bytes | None, str]:
        if ref is None:
            return None, ""
        try:
            from winrt.windows.storage.streams import Buffer, InputStreamOptions

            stream = await ref.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > 32 * 1024 * 1024:
                return None, ""
            buf = Buffer(size)
            await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            data = bytes(buf)
            if not data:
                return None, ""
            return data, hashlib.sha1(data).hexdigest()[:16]
        except Exception:
            return None, ""

    # ---- transport controls -------------------------------------------

    def _call(self, coro_name: str, *args) -> bool:
        """Invoke a session method on the WinRT loop and wait briefly."""
        sess = getattr(self, "_session", None)
        loop = self._loop
        if sess is None or loop is None:
            return False

        async def run():
            fn = getattr(sess, coro_name, None)
            if fn is None:
                return False
            try:
                return bool(await fn(*args))
            except Exception:
                return False

        try:
            fut = asyncio.run_coroutine_threadsafe(run(), loop)
            return bool(fut.result(timeout=4))
        except Exception:
            return False

    def play_pause(self) -> bool:
        return self._call("try_toggle_play_pause_async")

    def play(self) -> bool:
        return self._call("try_play_async")

    def pause(self) -> bool:
        return self._call("try_pause_async")

    def next(self) -> bool:
        return self._call("try_skip_next_async")

    def previous(self) -> bool:
        return self._call("try_skip_previous_async")

    def stop_playback(self) -> bool:
        return self._call("try_stop_async")

    def seek(self, seconds: float) -> bool:
        # SMTC positions are in 100-nanosecond ticks.
        ticks = int(max(0.0, float(seconds)) * 10_000_000)
        return self._call("try_change_playback_position_async", ticks)

    def set_shuffle(self, on: bool) -> bool:
        return self._call("try_change_shuffle_active_async", bool(on))

    def set_repeat(self, mode: str) -> bool:
        idx = {"none": 0, "track": 1, "list": 2}.get(mode, 0)
        return self._call("try_change_auto_repeat_mode_async", idx)
