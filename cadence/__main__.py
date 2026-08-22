"""Entry point: python -m cadence"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from . import win_effects
from .app import LAYOUT_SIZES, WEB_DIR, Api, CadenceApp


def _post_show(app: CadenceApp, window) -> None:
    """Native window polish, once the HWND actually exists."""
    for _ in range(40):
        if win_effects.find_hwnd(window):
            break
        time.sleep(0.05)
    try:
        win_effects.apply_all(window, app.settings.section("window"))
    except Exception:
        pass


def _on_screen(x: int, y: int) -> bool:
    """Is this point inside the virtual desktop, with room for a title bar?

    A minimised window reports -32000,-32000. Persisting that would make
    the next launch invisible, so positions are validated both on save
    and on restore.
    """
    import ctypes

    user32 = ctypes.windll.user32
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if vw <= 0 or vh <= 0:
        return False
    return (vx - 64) <= x <= (vx + vw - 64) and vy <= y <= (vy + vh - 64)


def _save_position(app: CadenceApp, window) -> None:
    if not app.settings.section("window").get("remember_position", True):
        return
    try:
        x, y = int(window.x), int(window.y)
    except Exception:
        return
    if not _on_screen(x, y):
        return  # minimised or dragged off the desktop; keep the old value
    app.settings.update({"window": {"x": x, "y": y}})


def _setup_logging() -> None:
    """A frozen, windowed build has nowhere to print, so log to a file."""
    import logging

    from .config import config_dir

    logging.basicConfig(
        filename=str(config_dir() / "cadence.log"),
        filemode="w",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.info("cadence starting; frozen=%s", getattr(sys, "frozen", False))
    logging.info("WEB_DIR=%s exists=%s", WEB_DIR, (WEB_DIR / "index.html").is_file())

    def excepthook(exc_type, exc, tb):
        logging.error("unhandled exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = excepthook


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cadence", description="Apple Music companion")
    ap.add_argument("--debug", action="store_true", help="open devtools")
    ap.add_argument("--layout", choices=sorted(LAYOUT_SIZES), help="start layout")
    ap.add_argument("--view", choices=("player", "stats", "settings", "fix"),
                    default="player")
    args = ap.parse_args(argv)

    _setup_logging()

    try:
        import webview
    except ImportError:
        print("pywebview is not installed. Run: pip install -r requirements.txt")
        return 1

    app = CadenceApp()
    if args.layout:
        app.settings.update({"window": {"layout": args.layout}})
    app.view = args.view
    app.start_backend()

    wcfg = app.settings.section("window")
    layout = wcfg.get("layout", "card")
    w, h = LAYOUT_SIZES.get(layout, LAYOUT_SIZES["card"])
    scale = float(wcfg.get("scale", 1.0))
    if args.view != "player":
        w, h = 980, 700

    # Refuse a stored position that no longer lands on a connected display.
    sx, sy = wcfg.get("x"), wcfg.get("y")
    if sx is None or sy is None or not _on_screen(int(sx), int(sy)):
        sx = sy = None

    window = webview.create_window(
        "Cadence",
        str(WEB_DIR / "index.html"),
        js_api=Api(app),
        width=int(w * scale),
        height=int(h * scale),
        min_size=(300, 96),
        frameless=bool(wcfg.get("frameless", True)),
        easy_drag=False,
        on_top=bool(wcfg.get("always_on_top", True)),
        resizable=True,
        background_color="#0d0d12",
        x=sx,
        y=sy,
    )
    app.window = window

    window.events.closing += lambda: _save_position(app, window)

    def on_start():
        threading.Thread(
            target=_post_show, args=(app, window), daemon=True
        ).start()

    try:
        webview.start(on_start, debug=args.debug)
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
