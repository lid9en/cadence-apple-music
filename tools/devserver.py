"""Serve the Cadence UI in a normal browser with a mocked Python bridge.

Lets you iterate on the interface without launching the WebView, and makes
the panels inspectable with ordinary browser dev tools.

    python tools/devserver.py            # http://127.0.0.1:8765
    python tools/devserver.py --port 9000

The mock mirrors the real `cadence.app.Api` surface. It is a development
aid only; nothing here ships in the app.
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "cadence" / "web"

MOCK_JS = r"""
/* Mock of window.pywebview.api for browser-based UI development. */
(function () {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const SETTINGS = {
    source: { mode: "apple", pinned_aumid: "", poll_ms: 200 },
    window: {
      layout: "card", always_on_top: true, click_through: false,
      frameless: true, transparent: false, opacity: 1, scale: 1,
      corner_radius: 18, remember_position: true, x: null, y: null,
      snap_to_edges: true, hide_when_paused: false, hide_when_fullscreen: false,
    },
    theme: {
      name: "midnight", adaptive: true, adaptive_strength: 0.85,
      accent_override: "", font: "system", font_scale: 1,
      background_art: true, background_art_blur: 42,
      background_art_opacity: 0.5, custom_css: "",
    },
    display: {
      show_artwork: true, show_progress: true, show_controls: true,
      show_meta: true, show_lyrics: true, show_source_badge: true,
      marquee_long_titles: true, time_style: "elapsed",
    },
    lyrics: { enabled: true, provider: "lrclib", synced: true, offset_ms: 0,
              lines_visible: 5 },
    enrich: { enabled: true, artwork_size: 1000, country: "us", cache_days: 30 },
    history: { enabled: true, min_seconds: 60, min_fraction: 0.5,
               ignore_sources: [] },
    hotkeys: {
      enabled: true,
      bindings: {
        play_pause: "Ctrl+Alt+Space", next: "Ctrl+Alt+Right",
        previous: "Ctrl+Alt+Left", toggle_window: "Ctrl+Alt+M",
        toggle_click_through: "Ctrl+Alt+T", cycle_layout: "Ctrl+Alt+L",
        show_stats: "Ctrl+Alt+S",
      },
    },
  };

  function deepMerge(base, over) {
    for (const k of Object.keys(over || {})) {
      if (over[k] && typeof over[k] === "object" && !Array.isArray(over[k])) {
        base[k] = deepMerge(base[k] || {}, over[k]);
      } else base[k] = over[k];
    }
    return base;
  }

  const LYRICS = [
    "Sitting in the dark again", "Counting all the ways to fall",
    "Every window looks the same", "From the inside of a wall",
    "And I don't mind the quiet now", "It says more than you ever did",
    "So play it once more, louder", "Before the morning gets here",
  ].map((text, i) => ({ t: i * 11 + 4, text }));

  // 1x1 gradient stand-in so artwork paths render.
  const ART =
    "data:image/svg+xml;base64," +
    btoa(`<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600">
      <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#7b2ff7"/><stop offset="1" stop-color="#f107a3"/>
      </linearGradient></defs><rect width="600" height="600" fill="url(#g)"/>
      <circle cx="300" cy="300" r="110" fill="#12101a"/>
      <circle cx="300" cy="300" r="18" fill="#f107a3"/></svg>`);

  let MOCK_REMOTE = { running: false, port: 8899, urls: [], primary_url: "",
                      qr_svg: "", error: "" };
  let MOCK_PHONE_STATE = { connected: false, device_id: "",
                           device_name: "", error: "" };
  const MOCK_PHONES = [
    { id: "bt-1", name: "Elliot's iPhone", enabled: true },
    { id: "bt-2", name: "iPad", enabled: false },
  ];

  let start = Date.now();
  let view = "player";

  const rnd = (n) => Math.floor(Math.random() * n);
  const artists = ["Radiohead", "Burial", "Panchiko", "Aphex Twin",
                   "Boards of Canada", "Portishead", "Björk", "Kendrick Lamar"];
  const mkRank = (n, key) =>
    Array.from({ length: n }, (_, i) => ({
      [key]: artists[i % artists.length],
      artist: artists[(i + 3) % artists.length],
      title: "Track " + (i + 1),
      album: "Album " + (i + 1),
      plays: 140 - i * 9 - rnd(5),
      seconds: (140 - i * 9) * 210,
      ts: Math.floor(Date.now() / 1000) - i * 5400,
      listened: 200,
    }));

  const heat = [];
  for (let i = 0; i < 182; i++) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    if (Math.random() > 0.25) {
      heat.push({ day: d.toISOString().slice(0, 10), plays: 1 + rnd(14) });
    }
  }

  window.pywebview = {
    api: {
      async get_state() {
        const pos = ((Date.now() - start) / 1000) % 245;
        const li = LYRICS.reduce((a, l, i) => (l.t <= pos ? i : a), -1);
        return {
          connected: true, source: "AppleInc.AppleMusicWin_x!App",
          source_label: "Apple Music", is_apple: true,
          title: "Weird Fishes / Arpeggi", artist: "Radiohead",
          album: "In Rainbows", status: "Playing", playing: true,
          position: pos, duration: 245, shuffle: false, repeat: "none",
          can_next: true, can_prev: true, can_seek: true,
          track_key: "mock-1", art_token: "mock-art",
          available_sources: [
            { aumid: "AppleInc.AppleMusicWin_x!App", label: "Apple Music",
              status: "Playing" },
            { aumid: "Spotify.exe", label: "Spotify", status: "Paused" },
          ],
          meta: { matched: true, genre: "Alternative", year: "2007",
                  apple_url: "https://music.apple.com" },
          palette: { bg: "#1a1030", bg2: "#271a45", fg: "#f5f5f7",
                     muted: "#a99cc4", accent: "#c04bff",
                     accent_fg: "#ffffff", is_dark: true },
          lyrics: { found: true, synced: true, active: li, lines: LYRICS },
          view,
        };
      },
      async get_artwork() { return { token: "mock-art", uri: ART }; },
      async get_settings() { return JSON.parse(JSON.stringify(SETTINGS)); },
      async update_settings(patch) {
        deepMerge(SETTINGS, patch);
        return JSON.parse(JSON.stringify(SETTINGS));
      },
      async reset_settings() { return JSON.parse(JSON.stringify(SETTINGS)); },
      async get_hotkey_status() {
        return { failures: { show_stats: "Ctrl+Alt+S is already taken by another app" } };
      },
      async control(action, value) { return { ok: true, action, value }; },
      async open_external() { return { ok: true }; },
      async set_view(v) { view = v; return { view: v }; },
      async window_action() { return { ok: true }; },
      async open_settings_folder() { return { ok: true }; },
      async clear_history() { return { ok: true }; },
      async get_stats() {
        await sleep(120);
        return {
          totals: { plays: 952, minutes: 3186, hours: 53.1, artists: 8,
                    tracks: 24, skips: 187 },
          top_artists: mkRank(8, "artist"),
          top_tracks: mkRank(8, "title"),
          top_albums: mkRank(6, "album"),
          recent: mkRank(15, "title"),
          heatmap: heat,
          by_hour: Array.from({ length: 24 }, (_, h) =>
            Math.round(40 * Math.exp(-Math.pow(h - 21, 2) / 22)) + rnd(4)),
        };
      },
      async fix_scan() {
        await sleep(200);
        return {
          installed: true, version: "1.1540.23042.0", running: true,
          processes: ["AppleMusic", "AMPLibraryAgent"],
          skip_rate: { window_seconds: 300, skips: 54, plays: 0,
                       median_skip_seconds: 0.21, storm: true },
          findings: [
            { id: "subscription", severity: "warn",
              title: "First: confirm the subscription is active",
              detail: "Apple Music → Account → Manage Subscription.",
              note: "Costs ten seconds to rule out and is the most common cause.",
              remedy: null },
            { id: "fairplay", severity: "error",
              title: "FairPlay key store is empty",
              detail: "SC Info.sidb — 0 bytes, 2026-08-22 22:40",
              note: "A zero-byte file means it holds no keys at all.",
              remedy: "reset_identity",
              remedy_label: "Reset identity + FairPlay (backs up first)" },
            { id: "adi", severity: "warn",
              title: "Apple device identity (ADI) store",
              detail: "adi.pb — 87 bytes, 2025-02-27",
              remedy: "reset_identity",
              remedy_label: "Reset identity + FairPlay (backs up first)" },
            { id: "timesync", severity: "warn",
              title: "Windows Time service is not running",
              detail: "w32time: Stopped · clock offset +4.7s",
              remedy: "start_timesync",
              remedy_label: "Start time sync and resync now" },
            { id: "playready", severity: "info",
              title: "PlayReady DRM store", detail: "1 file(s), 516 KB",
              note: "Not involved when the failure is 7505.",
              remedy: "reset_playready",
              remedy_label: "Reset PlayReady store (backs up first)" },
          ],
        };
      },
      async fix_logs() {
        await sleep(400);
        return {
          available: true,
          scanned: ["AMPLibraryAgent_2026-08-22.etl", "AppleMusic_2026-08-22.etl"],
          verdict: "store_request",
          summary: "Apple Music asked Apple for each track and was refused.",
          findings: [
            { code: "7505", count: 90, label: "store-request error 7505",
              explanation: "Apple refused to hand over the playback asset.",
              sample: "storereq> ***ERROR*** StoreAppleMusicDownloadAndLease(...) 7505" },
            { code: "7608", count: 57, label: "store-request error 7608",
              explanation: "Another store request failure.",
              sample: "Assertion failure: err == (7608)" },
          ],
        };
      },
      async fix_watch(seconds) {
        await sleep(600);
        return { seconds, verdict: "internal",
                 advice: "Nothing pressed next -- Apple Music abandoned each track.",
                 track_changes: 31, next_key_events: 0,
                 changes_preceded_by_key: 0, median_gap_seconds: 0.21,
                 injected_keys: 0, events: [], changes: [] };
      },
      async fix_apply(remedy) { return { ok: true, message: "Mock: " + remedy }; },
      async remote_status() {
        return MOCK_REMOTE;
      },
      async remote_start() {
        MOCK_REMOTE = { running: true, port: 8899,
          urls: ["http://192.168.10.150:8899/?t=demo"],
          primary_url: "http://192.168.10.150:8899/?t=demo",
          qr_svg: '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><rect width="120" height="120" fill="#fff"/><rect x="10" y="10" width="30" height="30"/><rect x="80" y="10" width="30" height="30"/><rect x="10" y="80" width="30" height="30"/><rect x="55" y="55" width="12" height="12"/></svg>',
          error: "" };
        return { ok: true, ...MOCK_REMOTE };
      },
      async remote_stop() {
        MOCK_REMOTE = { running: false, port: 8899, urls: [], primary_url: "",
                        qr_svg: "", error: "" };
        return { ok: true, ...MOCK_REMOTE };
      },
      async phone_devices() {
        await sleep(150);
        return {
          devices: MOCK_PHONES,
          status: MOCK_PHONE_STATE,
        };
      },
      async phone_connect(id, name) {
        await sleep(500);
        MOCK_PHONE_STATE = { connected: true, device_id: id,
                             device_name: name, error: "" };
        return { ok: true, status: "success",
                 message: name + " is now playing through this PC." };
      },
      async phone_disconnect() {
        MOCK_PHONE_STATE = { connected: false, device_id: "",
                             device_name: "", error: "" };
        return { ok: true };
      },
      async phone_arm(id, name, seconds) {
        MOCK_PHONE_STATE = { connected: false, device_id: id,
          device_name: name, error: "", arming: true,
          arming_seconds_left: seconds || 90 };
        return { ok: true, armed: true,
                 message: "Waiting for your phone." };
      },
      async phone_cancel_arm() {
        MOCK_PHONE_STATE = { connected: false, device_id: "", device_name: "",
                             error: "", arming: false, arming_seconds_left: 0 };
        return { ok: true };
      },
      async phone_status() {
        if (MOCK_PHONE_STATE.arming) {
          MOCK_PHONE_STATE.arming_seconds_left -= 1;
          if (MOCK_PHONE_STATE.arming_seconds_left <= 86) {
            MOCK_PHONE_STATE = { connected: true,
              device_id: MOCK_PHONE_STATE.device_id,
              device_name: MOCK_PHONE_STATE.device_name, error: "",
              arming: false, arming_seconds_left: 0 };
          }
        }
        return MOCK_PHONE_STATE;
      },
    },
  };

  window.dispatchEvent(new Event("pywebviewready"));
})();
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, fmt, *args):  # quieter console
        pass

    def do_GET(self):
        if self.path.startswith("/__mock.js"):
            body = MOCK_JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path in ("/", "/index.html"):
            html = (WEB / "index.html").read_text(encoding="utf-8")
            html = html.replace(
                '<script src="themes.js">',
                '<script src="/__mock.js"></script>\n<script src="themes.js">',
            )
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        super().do_GET()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    # NOT allow_reuse_address: on Windows that behaves like SO_REUSEPORT,
    # so a second server binds the same port and requests are handed to
    # whichever one the OS feels like -- you end up testing stale code.
    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"Cadence UI (mocked) on http://127.0.0.1:{args.port}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
