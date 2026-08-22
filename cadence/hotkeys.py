"""System-wide hotkeys via RegisterHotKey.

Windows requires that hotkeys be registered on, and delivered to, the
same thread that pumps the message loop, so everything lives on one
dedicated thread. Re-binding from the settings UI posts a request onto
that thread rather than touching the API cross-thread.
"""

from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001

MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN,
}

NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2,
    "mediaplaypause": 0xB3,
}
for _i in range(1, 25):
    NAMED_KEYS[f"f{_i}"] = 0x6F + _i


def parse(binding: str) -> tuple[int, int] | None:
    """'Ctrl+Alt+Right' -> (modifiers, vk). None if unparseable."""
    if not binding or not binding.strip():
        return None
    mods, vk = 0, None
    for part in binding.replace("-", "+").split("+"):
        p = part.strip().lower()
        if not p:
            continue
        if p in MODIFIERS:
            mods |= MODIFIERS[p]
            continue
        if p in NAMED_KEYS:
            vk = NAMED_KEYS[p]
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
        else:
            return None
    if vk is None or mods == 0:
        # Require a modifier: a bare letter would swallow it system-wide.
        return None
    return mods, vk


class HotkeyManager:
    def __init__(self, on_action: Callable[[str], None]):
        self.on_action = on_action
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._registered: dict[int, str] = {}   # id -> action
        self.failures: dict[str, str] = {}      # action -> reason

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._main, name="cadence-hotkeys", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def apply(self, bindings: dict[str, str], enabled: bool = True) -> None:
        """Queue a re-bind; performed on the hotkey thread."""
        self._q.put(("apply", dict(bindings or {}), bool(enabled)))

    # ---- thread internals ---------------------------------------------

    def _unregister_all(self) -> None:
        for hk_id in list(self._registered):
            user32.UnregisterHotKey(None, hk_id)
        self._registered.clear()

    def _register(self, bindings: dict[str, str], enabled: bool) -> None:
        self._unregister_all()
        self.failures = {}
        if not enabled:
            return
        for i, (action, combo) in enumerate(sorted(bindings.items()), start=1):
            parsed = parse(combo)
            if not parsed:
                if combo:
                    self.failures[action] = f"cannot parse {combo!r}"
                continue
            mods, vk = parsed
            if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                self._registered[i] = action
            else:
                err = ctypes.get_last_error()
                self.failures[action] = (
                    f"{combo} is already taken by another app"
                    if err == 1409 else f"{combo} failed (error {err})"
                )

    def _main(self) -> None:
        msg = wintypes.MSG()
        while not self._stop.is_set():
            # Drain rebind requests first.
            try:
                while True:
                    kind, bindings, enabled = self._q.get_nowait()
                    if kind == "apply":
                        self._register(bindings, enabled)
            except queue.Empty:
                pass

            got = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE)
            if got:
                if msg.message == WM_HOTKEY:
                    action = self._registered.get(int(msg.wParam))
                    if action:
                        try:
                            self.on_action(action)
                        except Exception:
                            pass
                continue
            # Nothing pending: sleep briefly so the thread stays cheap.
            user32.MsgWaitForMultipleObjectsEx(0, None, 40, 0x04FF, 0)

        self._unregister_all()
