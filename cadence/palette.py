"""Derive a colour scheme from album artwork.

Feeds the "adaptive" theme: the player re-tints itself to whatever is
playing. The goal is a background dark enough for white text, plus one
genuinely vivid accent, while never letting a muddy sleeve produce an
unreadable UI -- contrast is enforced at the end, not hoped for.
"""

from __future__ import annotations

import colorsys
import io
from typing import Any

DEFAULT = {
    "bg": "#12121a",
    "bg2": "#1b1b26",
    "fg": "#f5f5f7",
    "muted": "#a1a1aa",
    "accent": "#fa2d48",
    "accent_fg": "#ffffff",
    "is_dark": True,
}


def _hex(rgb) -> str:
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _luminance(rgb) -> float:
    """WCAG relative luminance."""
    def chan(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _set_value(rgb, value):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    r, g, b = colorsys.hsv_to_rgb(h, s, value)
    return (r * 255, g * 255, b * 255)


def _boost(rgb, sat_floor=0.55, val_floor=0.62):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    r, g, b = colorsys.hsv_to_rgb(h, max(s, sat_floor), max(v, val_floor))
    return (r * 255, g * 255, b * 255)


def swatches(image_bytes: bytes, count: int = 10) -> list[tuple[tuple, int]]:
    """[(rgb, pixel_count)] most common first. [] if the image is unreadable."""
    try:
        from PIL import Image
    except ImportError:
        return []
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")
        img.thumbnail((128, 128))
        q = img.quantize(colors=count, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette() or []
        out = []
        for n, idx in sorted(q.getcolors() or [], reverse=True):
            base = idx * 3
            if base + 2 < len(pal):
                out.append(((pal[base], pal[base + 1], pal[base + 2]), n))
        return out
    except Exception:
        return []


def from_artwork(image_bytes: bytes | None, dark: bool = True) -> dict[str, Any]:
    """Build a full palette from artwork, falling back to DEFAULT."""
    if not image_bytes:
        return dict(DEFAULT)

    sw = swatches(image_bytes)
    if not sw:
        return dict(DEFAULT)

    total = sum(n for _, n in sw) or 1

    # Accent: vivid and reasonably present, not merely the biggest blob.
    def accent_score(item):
        rgb, n = item
        _, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        share = n / total
        return (s ** 1.5) * (0.35 + v) * (0.25 + share)

    accent = _boost(max(sw, key=accent_score)[0])

    # Background: the dominant colour, pushed dark (or light) and calmed.
    dominant = sw[0][0]
    h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in dominant))
    if dark:
        base = _set_value(dominant, 0.14)
        base = _mix(base, (18, 18, 26), 0.45)
        bg2 = _mix(_set_value(dominant, 0.22), (26, 26, 36), 0.4)
        fg = (245, 245, 247)
    else:
        base = _mix(_set_value(dominant, 0.97), (255, 255, 255), 0.55)
        bg2 = _mix(_set_value(dominant, 0.92), (245, 245, 250), 0.45)
        fg = (26, 26, 30)

    # Guarantee readable body text no matter what the sleeve looks like.
    if _contrast(fg, base) < 7.0:
        fg = (255, 255, 255) if _luminance(base) < 0.4 else (12, 12, 14)

    # And a legible accent: nudge it until it clears AA against the bg.
    guard = 0
    while _contrast(accent, base) < 3.4 and guard < 12:
        accent = _boost(_mix(accent, fg, 0.14), sat_floor=0.45, val_floor=0.5)
        guard += 1

    accent_fg = (255, 255, 255) if _luminance(accent) < 0.45 else (10, 10, 12)
    muted = _mix(fg, base, 0.42)

    return {
        "bg": _hex(base),
        "bg2": _hex(bg2),
        "fg": _hex(fg),
        "muted": _hex(muted),
        "accent": _hex(accent),
        "accent_fg": _hex(accent_fg),
        "is_dark": bool(dark),
        "hue": round(h * 360),
        "sat": round(s, 3),
    }
