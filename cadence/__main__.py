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


def _save_position(app: CadenceApp, window) -> None:
    if not app.settings.section("window").get("remember_position", True):
        return
    try:
        app.settings.update({"window": {"x": int(window.x), "y": int(window.y)}})
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cadence", description="Apple Music companion")
    ap.add_argument("--debug", action="store_true", help="open devtools")
    ap.add_argument("--layout", choices=sorted(LAYOUT_SIZES), help="start layout")
    ap.add_argument("--view", choices=("player", "stats", "settings", "fix"),
                    default="player")
    args = ap.parse_args(argv)

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
        x=wcfg.get("x"),
        y=wcfg.get("y"),
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
