"""Let the PC act as a Bluetooth speaker for a phone.

Normally Windows is a Bluetooth audio *source* -- it sends audio to your
headphones. `AudioPlaybackConnection` flips that: the PC becomes a *sink*,
so a phone playing Apple Music can send its audio here and the PC speakers
play it.

The useful side effect is that Windows republishes the phone's AVRCP
metadata as an ordinary media session, which means `media.py` already sees
the track, artwork and transport controls with no extra work. This module
only has to establish and hold the audio link.

All WinRT work happens on one dedicated thread with its own asyncio loop
and a multi-threaded COM apartment, matching the pattern in `media.py`.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

# AudioPlaybackConnectionOpenResultStatus, spelled out so the UI can
# explain a refusal instead of just failing.
OPEN_STATUS = {
    0: ("success", "Connected."),
    1: (
        "denied_by_system",
        "Windows refused the connection. Another app is usually already "
        "acting as the Bluetooth audio receiver -- close it and retry.",
    ),
    2: (
        "request_timed_out",
        "The phone did not answer in time. Make sure it is paired and that "
        "you selected this PC as the audio output on the phone.",
    ),
    3: ("unknown_failure", "The connection failed for an unknown reason."),
}


def choose_device(devices: list[dict], preference: dict) -> str | None:
    """Decide which paired phone to open an audio connection to.

    Args:
        devices: available audio-source devices, each
            {"id": str, "name": str, "enabled": bool}. May be empty.
        preference: the `phone_audio` settings section, containing
            "preferred_device_id" (str, may be "") and
            "auto_connect" (bool).

    Returns:
        The device id to connect to, or None to stay disconnected.
    """
    # Conservative by default: grabbing the audio sink redirects sound and
    # can take the receiver role from another app, so an unasked-for
    # connection is worse than none. Only ever act on an explicit opt-in,
    # and only for a device the user already chose.
    if not preference.get("auto_connect"):
        return None

    wanted = (preference.get("preferred_device_id") or "").strip()
    if not wanted:
        return None

    for d in devices:
        if d.get("id") == wanted and d.get("enabled"):
            return wanted
    return None


class PhoneAudioBridge:
    """Holds a single Bluetooth audio sink connection open."""

    def __init__(self, settings):
        self.settings = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.RLock()

        # The connection object must outlive the coroutine that made it;
        # letting it be collected tears the audio link down.
        self._conn = None
        self._connected_id: str = ""
        self._connected_name: str = ""
        self._last_error: str = ""

        self.on_change: Callable[[dict], None] | None = None

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="cadence-phone-audio", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=10)

    def stop(self) -> None:
        self.disconnect()
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
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            finally:
                self._loop = None

    def _run(self, coro, timeout: float = 20.0):
        """Marshal a coroutine onto the WinRT thread and wait for it.

        Starts the thread on demand: callers should not have to remember to
        call start() first. A coroutine that never gets scheduled has to be
        closed explicitly, or Python warns that it was never awaited.
        """
        if self._loop is None:
            self.start()
        if self._loop is None:
            coro.close()
            with self._lock:
                self._last_error = "Bluetooth audio thread failed to start."
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ---- discovery -----------------------------------------------------

    async def _list(self) -> list[dict]:
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.media.audio import AudioPlaybackConnection as APC

        selector = APC.get_device_selector()
        found = await DeviceInformation.find_all_async_aqs_filter(selector)
        return [
            {"id": d.id, "name": d.name or "Unknown device",
             "enabled": bool(d.is_enabled)}
            for d in found
        ]

    def list_devices(self) -> list[dict]:
        """Paired devices that can send audio to this PC. Never raises."""
        try:
            return self._run(self._list(), timeout=15) or []
        except Exception as e:
            with self._lock:
                self._last_error = f"Could not enumerate devices: {e}"
            return []

    # ---- connection ----------------------------------------------------

    async def _open(self, device_id: str, name: str) -> dict:
        from winrt.windows.media.audio import AudioPlaybackConnection as APC

        conn = APC.try_create_from_id(device_id)
        if conn is None:
            return {"ok": False, "status": "unavailable",
                    "message": "Windows would not create a connection for "
                               "that device. Re-pair the phone and retry."}

        result = await conn.open_async()
        status = int(getattr(result, "status", 3))
        key, message = OPEN_STATUS.get(status, OPEN_STATUS[3])
        if key != "success":
            try:
                conn.close()
            except Exception:
                pass
            return {"ok": False, "status": key, "message": message}

        # Opened only reserves the link; this is what starts audio flowing.
        await conn.start_async()

        with self._lock:
            self._conn = conn
            self._connected_id = device_id
            self._connected_name = name
            self._last_error = ""
        return {"ok": True, "status": "success",
                "message": f"{name} is now playing through this PC."}

    def connect(self, device_id: str, name: str = "") -> dict:
        """Open and start the audio link. Safe to call when already open."""
        if not device_id:
            return {"ok": False, "status": "no_device",
                    "message": "No device selected."}
        if self._connected_id == device_id and self._conn is not None:
            return {"ok": True, "status": "already_open",
                    "message": f"{self._connected_name} is already connected."}

        self.disconnect()
        try:
            result = self._run(self._open(device_id, name or device_id))
        except Exception as e:
            result = {"ok": False, "status": "error", "message": str(e)}
        if not result.get("ok"):
            with self._lock:
                self._last_error = result.get("message", "")
        self._notify()
        return result

    def disconnect(self) -> dict:
        with self._lock:
            conn, self._conn = self._conn, None
            self._connected_id = ""
            self._connected_name = ""
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._notify()
        return {"ok": True}

    def auto_connect(self) -> dict | None:
        """Apply the selection policy to whatever is currently available."""
        cfg = self.settings.section("phone_audio")
        if not cfg.get("enabled", False):
            return None
        devices = self.list_devices()
        chosen = choose_device(devices, cfg)
        if not chosen:
            return None
        name = next((d["name"] for d in devices if d["id"] == chosen), "")
        return self.connect(chosen, name)

    # ---- status --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._conn is not None,
                "device_id": self._connected_id,
                "device_name": self._connected_name,
                "error": self._last_error,
            }

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.status())
            except Exception:
                pass
