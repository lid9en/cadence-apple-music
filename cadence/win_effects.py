"""Native window polish that CSS cannot reach.

A frameless WebView window is a plain rectangle by default. The DWM
attributes here give it Windows 11 rounded corners and a Mica/Acrylic
backdrop, and the extended window styles provide real click-through and
whole-window opacity.

Every call is best-effort: on an older build the attribute is simply
ignored and the window still works.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

# DWM window attributes
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_SYSTEMBACKDROP_TYPE = 38

# DWM_WINDOW_CORNER_PREFERENCE
CORNER_DEFAULT, CORNER_DONOTROUND, CORNER_ROUND, CORNER_ROUNDSMALL = 0, 1, 2, 3

# DWM_SYSTEMBACKDROP_TYPE
BACKDROP_AUTO, BACKDROP_NONE, BACKDROP_MICA, BACKDROP_ACRYLIC, BACKDROP_TABBED = (
    0, 1, 2, 3, 4,
)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
LWA_ALPHA = 0x00000002

HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010

DWMWA_COLOR_NONE = 0xFFFFFFFE

if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_long = user32.GetWindowLongPtrW
    _set_long = user32.SetWindowLongPtrW
    _get_long.restype = ctypes.c_ssize_t
    _set_long.restype = ctypes.c_ssize_t
    _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
else:  # pragma: no cover - 32-bit fallback
    _get_long = user32.GetWindowLongW
    _set_long = user32.SetWindowLongW


def _dwm_set(hwnd, attr: int, value: int) -> bool:
    try:
        v = ctypes.c_int(int(value))
        res = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(attr), ctypes.byref(v), ctypes.sizeof(v)
        )
        return res == 0
    except Exception:
        return False


def find_hwnd(window) -> int | None:
    """Get the HWND for a pywebview window, however the backend exposes it."""
    native = getattr(window, "native", None)
    for attr in ("Handle", "handle", "winId"):
        h = getattr(native, attr, None)
        if h is None:
            continue
        try:
            return int(h() if callable(h) else h)
        except Exception:
            continue

    # Fall back to locating a top-level window of this process by title.
    title = getattr(window, "title", None)
    if not title:
        return None
    pid = ctypes.windll.kernel32.GetCurrentProcessId()
    found: list[int] = []

    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == title:
            found.append(hwnd)
            return False
        return True

    try:
        user32.EnumWindows(EnumProc(cb), 0)
    except Exception:
        return None
    return found[0] if found else None


def round_corners(hwnd, enabled: bool = True, small: bool = False) -> bool:
    pref = (CORNER_ROUNDSMALL if small else CORNER_ROUND) if enabled \
        else CORNER_DONOTROUND
    return _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, pref)


def dark_titlebar(hwnd, dark: bool = True) -> bool:
    return _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)


def hide_border(hwnd) -> bool:
    return _dwm_set(hwnd, DWMWA_BORDER_COLOR, DWMWA_COLOR_NONE)


def backdrop(hwnd, kind: str = "mica") -> bool:
    mapping = {
        "none": BACKDROP_NONE,
        "mica": BACKDROP_MICA,
        "acrylic": BACKDROP_ACRYLIC,
        "tabbed": BACKDROP_TABBED,
        "auto": BACKDROP_AUTO,
    }
    return _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, mapping.get(kind, BACKDROP_MICA))


def set_opacity(hwnd, opacity: float) -> bool:
    """opacity 0.0-1.0 applied to the whole window."""
    try:
        alpha = max(0, min(255, int(round(float(opacity) * 255))))
        style = _get_long(hwnd, GWL_EXSTYLE)
        _set_long(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
        return bool(
            user32.SetLayeredWindowAttributes(
                wintypes.HWND(hwnd), 0, ctypes.c_ubyte(alpha), LWA_ALPHA
            )
        )
    except Exception:
        return False


def set_click_through(hwnd, enabled: bool) -> bool:
    """Let mouse input fall through to whatever is behind the window."""
    try:
        style = _get_long(hwnd, GWL_EXSTYLE)
        if enabled:
            style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        _set_long(hwnd, GWL_EXSTYLE, style)
        return True
    except Exception:
        return False


def set_topmost(hwnd, on: bool) -> bool:
    try:
        return bool(
            user32.SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(HWND_TOPMOST if on else HWND_NOTOPMOST),
                0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        )
    except Exception:
        return False


def apply_all(window, cfg: dict) -> dict:
    """Apply the window section of settings. Returns what actually stuck."""
    hwnd = find_hwnd(window)
    if not hwnd:
        return {"hwnd": None}

    theme_dark = True
    results = {
        "hwnd": hwnd,
        "dark": dark_titlebar(hwnd, theme_dark),
        "rounded": round_corners(hwnd, int(cfg.get("corner_radius", 18)) > 0),
        "border": hide_border(hwnd),
        "backdrop": backdrop(hwnd, "mica" if cfg.get("transparent") else "none"),
        "opacity": set_opacity(hwnd, cfg.get("opacity", 1.0)),
        "click_through": set_click_through(hwnd, bool(cfg.get("click_through"))),
        "topmost": set_topmost(hwnd, bool(cfg.get("always_on_top", True))),
    }
    return results
