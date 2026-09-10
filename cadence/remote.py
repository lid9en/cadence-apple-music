"""A phone remote for Cadence, served over the local network.

iPhones will not send Bluetooth audio to a Windows PC, so the useful
direction is the other one: music plays on the PC as normal, and the phone
becomes the remote. Everything needed already exists -- the media engine
can read and drive the session -- so this module is just a small HTTP
front door onto it.

Security posture: this listens on the LAN, so every request must carry a
token that is generated once and travels in the URL. It is a household
convenience, not an authentication system: anyone who has the link can
control playback. It is therefore off by default, serves only a fixed set
of routes, and never touches the filesystem based on request paths.
"""

from __future__ import annotations

import io
import json
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

WEB_DIR = Path(__file__).parent / "web"


def make_token() -> str:
    return secrets.token_urlsafe(9)


def lan_addresses() -> list[str]:
    """Every IPv4 address a phone on the same network might reach."""
    found: list[str] = []

    # The address used to reach the internet is the one that usually works.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        found.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            addr = info[4][0]
            if addr not in found and not addr.startswith("127."):
                found.append(addr)
    except OSError:
        pass
    return found


def qr_svg(data: str, scale: int = 4) -> str:
    """QR code as inline SVG, so the settings page needs no image route."""
    try:
        import qrcode
    except ImportError:
        return ""
    try:
        q = qrcode.QRCode(border=2, box_size=1)
        q.add_data(data)
        q.make(fit=True)
        matrix = q.get_matrix()
    except Exception:
        return ""

    n = len(matrix)
    size = n * scale
    rects = []
    for y, row in enumerate(matrix):
        run_start = None
        for x in range(n + 1):
            dark = x < n and row[x]
            if dark and run_start is None:
                run_start = x
            elif not dark and run_start is not None:
                rects.append(
                    f'<rect x="{run_start * scale}" y="{y * scale}" '
                    f'width="{(x - run_start) * scale}" height="{scale}"/>'
                )
                run_start = None
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
        f'height="{size}" viewBox="0 0 {size} {size}" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<g fill="#000">{"".join(rects)}</g></svg>'
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "Cadence"
    sys_version = ""

    # Injected by RemoteServer.
    token: str = ""
    facade: Any = None

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ---- helpers -------------------------------------------------------

    def _authorised(self, query: dict) -> bool:
        supplied = (query.get("t", [""])[0]
                    or self.headers.get("X-Cadence-Token", ""))
        return bool(self.token) and secrets.compare_digest(supplied, self.token)

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # This is a private appliance page; keep it out of any embedding.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _deny(self) -> None:
        self._send(403, b"Cadence: bad or missing token.",
                   "text/plain; charset=utf-8")

    # ---- routes --------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"

        if not self._authorised(query):
            return self._deny()

        if route == "/":
            try:
                html = (WEB_DIR / "remote.html").read_text(encoding="utf-8")
            except OSError:
                return self._send(500, b"remote.html missing",
                                  "text/plain; charset=utf-8")
            return self._send(200, html.encode("utf-8"),
                              "text/html; charset=utf-8")

        if route == "/api/state":
            return self._json(self.facade.state())

        if route == "/api/art":
            data, mime = self.facade.artwork()
            if not data:
                return self._send(404, b"", "text/plain")
            return self._send(200, data, mime)

        if route == "/api/playlists":
            return self._json(self.facade.playlists())

        return self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            return self._deny()

        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            return self._send(413, b"Too large", "text/plain")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json({"ok": False, "error": "bad json"}, 400)

        if urlparse(self.path).path.rstrip("/") == "/api/control":
            action = str(payload.get("action", ""))
            value = payload.get("value")
            return self._json(self.facade.control(action, value))

        return self._send(404, b"Not found", "text/plain; charset=utf-8")


class RemoteServer:
    """Serves the phone remote on the LAN. Off unless explicitly started."""

    def __init__(self, settings, facade):
        self.settings = settings
        self.facade = facade
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._error = ""

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> dict:
        with self._lock:
            if self._httpd is not None:
                return {"ok": True, **self.status()}

            cfg = self.settings.section("remote")
            token = cfg.get("token") or make_token()
            port = int(cfg.get("port", 8899))

            handler = type("_BoundHandler", (_Handler,),
                           {"token": token, "facade": self.facade})
            try:
                # Deliberately not allow_reuse_address: on Windows that lets a
                # second server bind the same port and requests get handed to
                # whichever one the OS picks.
                httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
            except OSError as e:
                self._error = (
                    f"Could not listen on port {port}: {e}. "
                    "Another program may already be using it."
                )
                return {"ok": False, "error": self._error}

            httpd.daemon_threads = True
            self._httpd = httpd
            self._error = ""
            self.settings.update({"remote": {"token": token, "enabled": True}})

            self._thread = threading.Thread(
                target=httpd.serve_forever, name="cadence-remote",
                kwargs={"poll_interval": 0.5}, daemon=True)
            self._thread.start()

        return {"ok": True, **self.status()}

    def stop(self) -> dict:
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        self.settings.update({"remote": {"enabled": False}})
        return {"ok": True, **self.status()}

    # ---- info ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        cfg = self.settings.section("remote")
        running = self._httpd is not None
        port = int(cfg.get("port", 8899))
        token = cfg.get("token", "")
        urls = [f"http://{ip}:{port}/?t={token}" for ip in lan_addresses()] \
            if running and token else []
        return {
            "running": running,
            "port": port,
            "urls": urls,
            "primary_url": urls[0] if urls else "",
            "qr_svg": qr_svg(urls[0]) if urls else "",
            "error": self._error,
        }
