"""Diagnose and repair Apple Music for Windows skipping every track.

Three different faults produce the identical symptom -- the library races
past every song in a fraction of a second -- and they need opposite fixes,
so the job is telling them apart with evidence rather than guesswork:

  external      Something is spamming MEDIA_NEXT_TRACK at the system.
                `watch()` catches this with a media-key hook.

  store/identity Apple Music cannot obtain a playback asset for any track.
                Its own logs say so explicitly: `store-request error 7505`
                on every item, usually downstream of
                `ADIOTPRequest failed with error -45061`, meaning the
                device identity handshake with Apple failed. This is the
                common one, and no amount of clearing the local PlayReady
                cache touches it.

  local drm     A genuinely corrupt local licence store.

`scan()` is fast and read-only. `analyze_logs()` decodes Apple Music's own
ETL traces, which is where the real error codes live. `watch()` observes
live input. `apply()` performs a repair and always backs up first.
"""

from __future__ import annotations

import ctypes
import html
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

PACKAGE_FAMILY = "AppleInc.AppleMusicWin_nzyj5cx40ttqa"
PACKAGE_NAME = "AppleInc.AppleMusicWin"

# The library bundle. Its Library.musicdb also carries the download and
# redownload queue -- there is no separate queue file to clear.
MUSIC_LIBRARY = (Path.home() / "Music" / "Apple Music"
                 / "Apple Music Library.musiclibrary")

# Shared by legacy iTunes and the Store app -- which is exactly why having
# both installed can break the pair of them.
ADI_DIR = Path(r"C:\ProgramData\Apple Computer\iTunes\adi")
SC_INFO_DIRS = [
    Path(r"C:\ProgramData\Apple\SC Info"),
    Path(r"C:\ProgramData\Apple Computer\iTunes\SC Info"),
]

user32 = ctypes.WinDLL("user32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
HHOOK = ctypes.c_void_p
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
LLKHF_INJECTED = 0x10
LLKHF_LOWER_IL_INJECTED = 0x02
PM_REMOVE = 0x0001

MEDIA_KEYS = {
    0xAD: "VOLUME_MUTE",
    0xAE: "VOLUME_DOWN",
    0xAF: "VOLUME_UP",
    0xB0: "MEDIA_NEXT_TRACK",
    0xB1: "MEDIA_PREV_TRACK",
    0xB2: "MEDIA_STOP",
    0xB3: "MEDIA_PLAY_PAUSE",
}

# Error codes worth explaining in plain language when they show up.
KNOWN_ERRORS = {
    "7505": (
        "store-request error 7505",
        "Apple refused to hand over the playback asset for the track. This "
        "is the direct cause of the instant skipping: with no asset, the "
        "player gives up and advances immediately. It means the request was "
        "not accepted -- an inactive subscription, or a device that failed "
        "to identify itself to Apple.",
    ),
    "7608": (
        "store-request error 7608",
        "Another store request failure, usually alongside 7505.",
    ),
    "-45061": (
        "ADIOTPRequest failed with error -45061",
        "The device identity handshake with Apple failed. ADI is the "
        "provisioning data that proves this machine to Apple's servers; the "
        "OTP request is the time-based token built from it. When this "
        "fails, store requests go out unauthenticated and come back 7505.",
    ),
    "-42110": (
        "authorisation error -42110",
        "The machine is not authorised to play protected content.",
    ),
}


def package_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Packages" / PACKAGE_FAMILY


def logs_dir() -> Path:
    return package_dir() / "LocalState" / "Logs"


def _ps(cmd: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def running_processes() -> list[str]:
    out = _ps(
        "Get-Process AppleMusic,AMPLibraryAgent,iTunes,iTunesHelper "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name -Unique"
    )
    return [l.strip() for l in out.splitlines() if l.strip()]


def is_running() -> bool:
    return bool(running_processes())


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_fairplay() -> dict:
    """SC Info holds the FairPlay keys. A zero-byte .sidb means no keys."""
    details, broken = [], False
    for d in SC_INFO_DIRS:
        if not d.exists():
            details.append(f"{d} — missing")
            continue
        for f in sorted(d.glob("*")):
            try:
                size = f.stat().st_size
            except OSError:
                continue
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            details.append(f"{f.name} — {size} bytes, {mtime}")
            if f.suffix.lower() == ".sidb" and size == 0:
                broken = True
    return {
        "id": "fairplay",
        "severity": "error" if broken else "info",
        "title": ("FairPlay key store is empty" if broken
                  else "FairPlay key store"),
        "detail": " · ".join(details) or "No SC Info store found.",
        "remedy": "reset_identity",
        "remedy_label": "Reset identity + FairPlay (backs up first)",
        "note": (
            "SC Info.sidb is where Apple Music keeps the keys that decrypt "
            "protected tracks. A zero-byte file means it holds no keys at "
            "all, so every protected song fails the moment it starts."
            if broken else
            "This is where the keys that decrypt protected tracks live."
        ),
    }


def check_identity() -> dict:
    """ADI provisioning: the data ADIOTPRequest needs."""
    files, stale = [], False
    if ADI_DIR.exists():
        for f in sorted(ADI_DIR.glob("*")):
            try:
                st = f.stat()
            except OSError:
                continue
            age_days = (time.time() - st.st_mtime) / 86400
            stale = stale or age_days > 120
            files.append(
                f"{f.name} — {st.st_size} bytes, "
                f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d}"
            )
    return {
        "id": "adi",
        "severity": "warn" if (stale or not files) else "info",
        "title": "Apple device identity (ADI) store",
        "detail": " · ".join(files) or f"{ADI_DIR} — missing",
        "remedy": "reset_identity",
        "remedy_label": "Reset identity + FairPlay (backs up first)",
        "note": (
            "Shared by legacy iTunes and the Store app. If ADIOTPRequest is "
            "failing in the logs, this is the data it could not use."
        ),
    }


def check_library() -> dict:
    """The library bundle also holds the download / redownload queue.

    Apple Music keeps that queue inside Library.musicdb, a proprietary
    `hfma` container -- there is no separate queue file to clear. When the
    logs show repeated SendRedownloadIfNecessary and StoreAppleMusicDownload
    failures, a stale queue in here is a plausible cause, and rebuilding the
    bundle from the cloud library is the only supported way to clear it.
    """
    if not MUSIC_LIBRARY.exists():
        return {
            "id": "library",
            "severity": "info",
            "title": "Music library has not been created yet",
            "detail": str(MUSIC_LIBRARY),
            "remedy": None,
            "note": "It is rebuilt on next launch once you are signed in.",
        }

    db = MUSIC_LIBRARY / "Library.musicdb"
    size = db.stat().st_size if db.exists() else 0
    media = [f for f in MUSIC_LIBRARY.rglob("*")
             if f.suffix.lower() in (".m4p", ".m4a", ".aac", ".mp3")]
    return {
        "id": "library",
        "severity": "info",
        "title": "Music library and download queue",
        "detail": (
            f"Library.musicdb {size / 1024:.0f} KB, "
            f"{_dir_size(MUSIC_LIBRARY) / 1048576:.0f} MB total, "
            f"{len(media)} downloaded track file(s)"
        ),
        "remedy": "reset_library",
        "remedy_label": "Rebuild library (clears the download queue)",
        "note": (
            "Rebuilding clears any stuck download or redownload queue. Your "
            "library and playlists come back from iCloud on next launch. "
            "Anything you imported locally and never uploaded would be lost, "
            "so this checks for local audio files first and refuses if it "
            "finds any."
        ),
    }


def check_apple_trust() -> dict:
    """Apple's auth server uses Apple's own root; Windows must trust it."""
    found = _ps(
        "$n=0; foreach($s in 'Cert:\\LocalMachine\\Root','Cert:\\CurrentUser\\Root'){"
        "$n += (Get-ChildItem $s -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Subject -match 'Apple' }).Count }; $n"
    )
    try:
        count = int((found or "0").split()[0])
    except (ValueError, IndexError):
        count = 0
    missing = count == 0
    return {
        "id": "apple_root",
        "severity": "warn" if missing else "info",
        "title": ("No Apple root certificates in the Windows trust store"
                  if missing else f"{count} Apple root certificate(s) trusted"),
        "detail": (
            "gsa.apple.com (Apple's Grand Slam authentication server) "
            "presents a chain ending at 'Apple Root CA'. Windows currently "
            "has no Apple roots installed, so that chain cannot be "
            "completed." if missing else "Apple's chain can be validated."
        ),
        "remedy": "install_apple_root" if missing else None,
        "remedy_label": "How to install Apple Root CA",
        "note": (
            "Apple's own components normally install this. Its absence, "
            "together with an ADI failure in the logs, is consistent with "
            "the identity handshake failing."
            if missing else None
        ),
    }


def check_timesync() -> dict:
    status = _ps("(Get-Service w32time -ErrorAction SilentlyContinue).Status")
    skew = None
    try:
        import email.utils
        import urllib.request
        from datetime import timezone

        req = urllib.request.Request(
            "https://www.apple.com", method="HEAD",
            headers={"User-Agent": "Cadence"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            served = email.utils.parsedate_to_datetime(r.headers.get("Date"))
        skew = (datetime.now(timezone.utc) - served).total_seconds()
    except Exception:
        pass

    stopped = (status or "").strip().lower() != "running"
    bad_skew = skew is not None and abs(skew) > 60
    return {
        "id": "timesync",
        "severity": "error" if bad_skew else ("warn" if stopped else "info"),
        "title": ("Clock is out by more than a minute" if bad_skew else
                  "Windows Time service is not running" if stopped else
                  "Clock and time sync are healthy"),
        "detail": (
            f"w32time: {status or 'unknown'}"
            + (f" · clock offset {skew:+.1f}s vs apple.com" if skew is not None
               else " · could not measure offset")
        ),
        "remedy": "start_timesync" if (stopped or bad_skew) else None,
        "remedy_label": "Start time sync and resync now",
        "note": (
            "The device identity token is time-based, so a drifting clock "
            "eventually breaks it. Small offsets are harmless, but with the "
            "time service stopped the clock will keep drifting."
            if stopped or bad_skew else None
        ),
    }


def check_itunes_conflict() -> dict | None:
    out = _ps(
        "Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion"
        "\\Uninstall\\*','HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows"
        "\\CurrentVersion\\Uninstall\\*' -ErrorAction SilentlyContinue | "
        "Where-Object DisplayName -match 'iTunes' | "
        "Select-Object -ExpandProperty DisplayName -Unique"
    )
    names = [l.strip() for l in out.splitlines() if l.strip()]
    if not names:
        return None
    return {
        "id": "itunes_conflict",
        "severity": "warn",
        "title": "Legacy iTunes is installed alongside the Apple Music app",
        "detail": ", ".join(names),
        "remedy": None,
        "note": (
            "Both share the same device-identity and FairPlay stores under "
            "C:\\ProgramData\\Apple Computer\\iTunes. When those stores get "
            "into a bad state, both apps stay broken. If resetting identity "
            "fixes playback but it returns, uninstalling legacy iTunes is "
            "the durable fix."
        ),
    }


def check_input_sources() -> list[dict]:
    out: list[dict] = []
    avrcp_raw = _ps(
        "Get-PnpDevice -Class Bluetooth -Status OK -ErrorAction SilentlyContinue | "
        "Where-Object FriendlyName -like '*Avrcp*' | "
        "Select-Object -ExpandProperty FriendlyName"
    )
    avrcp = sorted({l.strip() for l in avrcp_raw.splitlines() if l.strip()})
    if avrcp:
        out.append({
            "id": "avrcp",
            "severity": "info",
            "title": f"{len(avrcp)} Bluetooth AVRCP transport(s) connected",
            "detail": ", ".join(avrcp),
            "remedy": None,
            "note": (
                "Each can send next-track to Windows. Only relevant if the "
                "live watch reports hardware key presses."
            ),
        })

    procs_raw = _ps(
        "Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "
        "'steam|SteelSeries|iCUE|Logi|lghub|Razer|Armoury|GHUB|SonyAudio|"
        "Headphones|OpenRGB|AutoHotkey|Nahimic' } | "
        "Select-Object -ExpandProperty Name -Unique"
    )
    procs = sorted({l.strip() for l in procs_raw.splitlines() if l.strip()})
    if procs:
        out.append({
            "id": "input_software",
            "severity": "info",
            "title": "Software that can synthesise media keys is running",
            "detail": ", ".join(procs),
            "remedy": None,
            "note": (
                "Only relevant if the live watch reports software-injected "
                "keys. Otherwise these are innocent."
            ),
        })
    return out


# ---------------------------------------------------------------------------
# Log analysis -- where the real answer lives
# ---------------------------------------------------------------------------


def analyze_logs(max_files: int = 5, consider: int = 12) -> dict[str, Any]:
    """Decode recent Apple Music ETL traces and pull out error codes.

    The newest files by timestamp are often the near-empty rolling logs
    written while the app sits idle, so candidates are ranked by recency
    but the small ones are skipped and several are read before giving up.
    """
    d = logs_dir()
    if not d.exists():
        return {"available": False, "reason": f"No log directory at {d}"}

    try:
        etls = sorted(
            d.glob("*.etl"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:consider]
    except OSError:
        etls = []
    if not etls:
        return {"available": False, "reason": "No .etl trace files yet."}

    # A trace with nothing in it cannot explain anything.
    candidates = [p for p in etls if p.stat().st_size > 8192] or etls

    counts: dict[str, int] = {}
    samples: dict[str, str] = {}
    scanned: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        for etl in candidates:
            if len(scanned) >= max_files:
                break
            xml = Path(tmp) / (etl.stem + ".xml")
            _ps(f"tracerpt '{etl}' -o '{xml}' -of XML -lr -y", timeout=120)
            if not xml.exists():
                continue
            scanned.append(etl.name)
            try:
                text = xml.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for msg in re.findall(r'StringMessage">([^<]{0,400})', text):
                low = msg.lower()
                if "error" not in low and "fail" not in low:
                    continue
                for code in KNOWN_ERRORS:
                    if code in msg:
                        counts[code] = counts.get(code, 0) + 1
                        samples.setdefault(code, html.unescape(msg).strip()[:240])

            # Enough evidence: stop burning time on more traces.
            if counts and len(scanned) >= 2:
                break

    findings = []
    for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        label, explain = KNOWN_ERRORS[code]
        findings.append({
            "code": code,
            "count": n,
            "label": label,
            "explanation": explain,
            "sample": samples.get(code, ""),
        })

    verdict = "clean"
    if counts.get("7505") or counts.get("7608"):
        verdict = "store_request"
    elif counts.get("-45061"):
        verdict = "identity"
    elif counts.get("-42110"):
        verdict = "authorisation"

    return {
        "available": True,
        "scanned": scanned,
        "findings": findings,
        "verdict": verdict,
        "summary": {
            "store_request": (
                "Apple Music asked Apple for each track and was refused. "
                "Check that the Apple Music subscription is currently "
                "active, then reset the device identity so the app can "
                "re-authenticate."
            ),
            "identity": (
                "The device identity handshake failed. Reset the identity "
                "store so the app re-provisions with Apple."
            ),
            "authorisation": (
                "This machine is not authorised to play protected content."
            ),
            "clean": (
                "No known playback error codes in the traces scanned. If the "
                "skipping is still happening, run the live watch -- it may "
                "be coming from outside Apple Music."
            ),
        }[verdict],
    }


# ---------------------------------------------------------------------------
# Read-only scan
# ---------------------------------------------------------------------------


def scan() -> dict[str, Any]:
    version = _ps(
        f"(Get-AppxPackage {PACKAGE_NAME} | Select-Object -First 1).Version"
    )
    findings: list[dict] = []

    if not version:
        findings.append({
            "id": "not_installed",
            "severity": "error",
            "title": "Apple Music for Windows is not installed",
            "detail": "Install it from the Microsoft Store first.",
            "remedy": None,
        })

    findings.append({
        "id": "subscription",
        "severity": "warn",
        "title": "First: confirm the subscription is active",
        "detail": (
            "Apple Music → Account → Manage Subscription. A lapsed or "
            "payment-failed subscription makes Apple refuse every track "
            "with the exact error this app finds in the logs (7505), and "
            "the library races past every song."
        ),
        "remedy": None,
        "note": (
            "This costs ten seconds to rule out and is the single most "
            "common cause. Nothing below can fix an inactive subscription."
        ),
    })

    findings.append(check_library())
    findings.append(check_fairplay())
    findings.append(check_identity())
    findings.append(check_apple_trust())
    findings.append(check_timesync())

    conflict = check_itunes_conflict()
    if conflict:
        findings.append(conflict)

    pkg = package_dir()
    cache = pkg / "LocalCache" / "Local"
    if cache.exists():
        findings.append({
            "id": "cache",
            "severity": "info",
            "title": "Apple Music cache",
            "detail": f"{_dir_size(cache) / 1048576:.1f} MB",
            "remedy": "clear_cache",
            "remedy_label": "Clear cache (backs up first)",
            "note": "Rebuilt automatically. Rarely the cause on its own.",
        })

    pr = pkg / "LocalCache" / "PlayReady"
    if pr.exists():
        findings.append({
            "id": "playready",
            "severity": "info",
            "title": "PlayReady DRM store",
            "detail": f"{len(list(pr.glob('**/*')))} file(s), "
                      f"{_dir_size(pr) / 1024:.0f} KB",
            "remedy": "reset_playready",
            "remedy_label": "Reset PlayReady store (backs up first)",
            "note": (
                "Worth trying only if the logs show no store-request errors. "
                "When the failure is 7505, this store is not involved and "
                "clearing it changes nothing."
            ),
        })

    findings.extend(check_input_sources())

    return {
        "installed": bool(version),
        "version": version,
        "running": is_running(),
        "processes": running_processes(),
        "package_dir": str(pkg),
        "findings": findings,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# Live watch
# ---------------------------------------------------------------------------


class MediaKeyWatcher:
    """Logs ONLY media/volume virtual-key codes. Never ordinary keystrokes."""

    def __init__(self):
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._hook = None
        self._ref = None
        self._t0 = time.monotonic()

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _main(self) -> None:
        class KBD(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        HOOKPROC = ctypes.WINFUNCTYPE(
            LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, wintypes.HMODULE, wintypes.DWORD
        ]
        user32.SetWindowsHookExW.restype = HHOOK
        user32.CallNextHookEx.argtypes = [
            HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        ]
        user32.CallNextHookEx.restype = LRESULT
        user32.UnhookWindowsHookEx.argtypes = [HHOOK]

        def proc(nCode, wParam, lParam):
            if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(lParam, ctypes.POINTER(KBD)).contents
                name = MEDIA_KEYS.get(kb.vkCode)
                if name:
                    self.events.append({
                        "t": round(time.monotonic() - self._t0, 3),
                        "key": name,
                        "injected": bool(kb.flags & LLKHF_INJECTED),
                        "lower_il": bool(kb.flags & LLKHF_LOWER_IL_INJECTED),
                    })
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._ref = HOOKPROC(proc)
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._ref, None, 0)
        if not self._hook:
            return

        msg = wintypes.MSG()
        while not self._stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                user32.MsgWaitForMultipleObjectsEx(0, None, 30, 0x04FF, 0)

        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None


def watch(seconds: int = 20, engine=None) -> dict[str, Any]:
    seconds = max(5, min(120, int(seconds)))
    watcher = MediaKeyWatcher()
    watcher.start()

    t0 = time.monotonic()
    changes: list[dict] = []
    last_key = None

    while time.monotonic() - t0 < seconds:
        if engine is not None:
            st = engine.state()
            key = st.get("track_key") or ""
            if key and key != last_key:
                if last_key is not None:
                    changes.append({
                        "t": round(time.monotonic() - t0, 3),
                        "title": st.get("title", ""),
                        "artist": st.get("artist", ""),
                        "source": st.get("source_label", ""),
                    })
                last_key = key
        time.sleep(0.08)

    watcher.stop()
    keys = [e for e in watcher.events if e["key"] == "MEDIA_NEXT_TRACK"]
    keyed = sum(
        1 for c in changes
        if any(k["t"] <= c["t"] and c["t"] - k["t"] < 0.5 for k in keys)
    )
    gaps = [b["t"] - a["t"] for a, b in zip(changes, changes[1:])]
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else None

    if not changes:
        verdict, advice = "no_data", (
            "No track changes were seen. Start playback in Apple Music while "
            "the watch is running."
        )
    elif keyed >= max(1, len(changes) // 2):
        if any(k["injected"] for k in keys):
            verdict, advice = "external_software", (
                "An application is sending next-track keys. Close controller "
                "and peripheral software one at a time."
            )
        else:
            verdict, advice = "external_hardware", (
                "Real media-key presses are arriving from hardware. "
                "Disconnect Bluetooth headsets and controllers, then retest."
            )
    else:
        verdict, advice = "internal", (
            "Nothing pressed next -- Apple Music abandoned each track by "
            "itself. Run the log analysis: if it reports store-request "
            "error 7505, the tracks are being refused by Apple, and the fix "
            "is the subscription plus a device-identity reset, not the "
            "local DRM cache."
        )

    return {
        "seconds": seconds,
        "verdict": verdict,
        "advice": advice,
        "track_changes": len(changes),
        "next_key_events": len(keys),
        "changes_preceded_by_key": keyed,
        "median_gap_seconds": round(median_gap, 3) if median_gap else None,
        "injected_keys": sum(1 for k in keys if k["injected"]),
        "events": watcher.events[-60:],
        "changes": changes[-40:],
    }


# ---------------------------------------------------------------------------
# Remedies -- always backed up, always explicitly confirmed by the caller
# ---------------------------------------------------------------------------


def _backup(src: Path, stamp: str) -> Path | None:
    if not src.exists():
        return None
    dst = src.parent / f"{src.name}.cadence-backup-{stamp}"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return dst


def _empty_dir(target: Path) -> tuple[int, list[str]]:
    removed, failed = 0, []
    for f in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if f.is_file():
                f.unlink()
                removed += 1
            elif f.is_dir():
                f.rmdir()
        except OSError as e:
            failed.append(f"{f.name}: {e.strerror}")
    return removed, failed


def _stop_apple(timeout: float = 3.0) -> None:
    _ps(
        "Stop-Process -Name AppleMusic,AMPLibraryAgent,iTunes,iTunesHelper "
        "-Force -ErrorAction SilentlyContinue"
    )
    time.sleep(timeout)


#: Remedies that delete user data. These need `destructive_ok=True` so the
#: function cannot be called casually -- including from a REPL while
#: "just checking that it works". Ask me how I know.
DESTRUCTIVE = frozenset({
    "reset_library", "reset_identity", "reset_playready", "clear_cache",
})


def apply(remedy: str, destructive_ok: bool = False) -> dict[str, Any]:
    if remedy in DESTRUCTIVE and not destructive_ok:
        return {
            "ok": False,
            "error": (
                f"{remedy!r} deletes user data. Pass destructive_ok=True to "
                "confirm you mean it. There is no dry run for this: if you "
                "are testing, point HOME at a scratch directory first."
            ),
        }

    pkg = package_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if remedy == "restart_app":
        _stop_apple(1.5)
        _ps(f"Start-Process 'shell:AppsFolder\\{PACKAGE_FAMILY}!App'")
        return {"ok": True, "message": "Apple Music restarted."}

    if remedy == "start_timesync":
        out = _ps(
            "Set-Service w32time -StartupType Automatic -ErrorAction SilentlyContinue; "
            "Start-Service w32time -ErrorAction SilentlyContinue; "
            "w32tm /resync /force 2>&1 | Out-String"
        )
        status = _ps("(Get-Service w32time -ErrorAction SilentlyContinue).Status")
        ok = (status or "").strip().lower() == "running"
        return {
            "ok": ok,
            "message": (
                f"Windows Time service is {status or 'unknown'}. "
                + (out.strip()[:200] if out else "")
                + ("" if ok else " Starting it may need administrator rights.")
            ),
        }

    if remedy == "install_apple_root":
        return {
            "ok": True,
            "message": (
                "Apple publishes its roots at apple.com/certificateauthority. "
                "Download 'Apple Root CA' (AppleIncRootCertificate.cer), "
                "double-click it, choose Install Certificate → Local Machine "
                "→ Trusted Root Certification Authorities. Installing a root "
                "certificate needs administrator rights, and Cadence will not "
                "do it for you — adding a trusted root is a decision you "
                "should make deliberately."
            ),
        }

    if remedy == "reset_library":
        if not MUSIC_LIBRARY.exists():
            return {"ok": False, "error": "There is no library to rebuild."}

        # Refuse rather than destroy anything that is not in the cloud.
        local = [f for f in MUSIC_LIBRARY.rglob("*")
                 if f.suffix.lower() in (".m4p", ".m4a", ".aac", ".mp3",
                                         ".wav", ".flac", ".aiff")]
        if local:
            return {
                "ok": False,
                "error": (
                    f"Found {len(local)} audio file(s) inside the library. "
                    "Those may not exist anywhere else, so this refuses to "
                    "delete them. Move them out first."
                ),
            }

        _stop_apple()
        try:
            backup = _backup(MUSIC_LIBRARY, stamp)
        except Exception as e:
            return {"ok": False, "error": f"backup failed, nothing removed: {e}"}

        removed, failed = _empty_dir(MUSIC_LIBRARY)
        try:
            MUSIC_LIBRARY.rmdir()
        except OSError:
            pass

        return {
            "ok": not failed,
            "message": (
                f"Cleared the library ({removed} file(s)). Backup kept as "
                f"{backup.name if backup else 'n/a'}. Launch Apple Music and "
                "sign in; it rebuilds from iCloud with an empty download "
                "queue. Delete the backup folder once playback works."
            ),
            "backup": str(backup) if backup else None,
            "failed": failed[:8],
        }

    if remedy == "reset_identity":
        targets = [d for d in ([ADI_DIR] + SC_INFO_DIRS) if d.exists()]
        if not targets:
            return {"ok": False, "error": "No identity or FairPlay stores found."}

        _stop_apple()

        backups, removed_total, failed_all = [], 0, []
        for t in targets:
            try:
                b = _backup(t, stamp)
                if b:
                    backups.append(str(b))
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"Backup of {t} failed, nothing removed: {e}",
                }
        for t in targets:
            n, failed = _empty_dir(t)
            removed_total += n
            failed_all.extend(failed)

        return {
            "ok": not failed_all,
            "message": (
                f"Removed {removed_total} file(s) from {len(targets)} store(s). "
                f"Backups: {', '.join(Path(b).name for b in backups)}. "
                "Now launch Apple Music, sign in if asked, and play a track — "
                "it will re-provision the device identity and FairPlay keys. "
                "If it still skips, the subscription itself is the problem."
            ),
            "backups": backups,
            "failed": failed_all[:8],
        }

    if remedy in ("reset_playready", "clear_cache"):
        target = (pkg / "LocalCache" / "PlayReady") if remedy == "reset_playready" \
            else (pkg / "LocalCache" / "Local")
        if not target.exists():
            return {"ok": False, "error": f"{target} does not exist."}

        _stop_apple(2.0)
        try:
            backup = _backup(target, stamp)
        except Exception as e:
            return {"ok": False, "error": f"backup failed, nothing removed: {e}"}

        removed, failed = _empty_dir(target)
        return {
            "ok": not failed,
            "message": (
                f"Removed {removed} file(s) from {target.name}. "
                f"Backup kept as {backup.name if backup else 'n/a'}."
            ),
            "backup": str(backup) if backup else None,
            "failed": failed[:8],
        }

    if remedy == "reregister":
        out = _ps(
            f"Get-AppxPackage {PACKAGE_NAME} | ForEach-Object "
            "{ Add-AppxPackage -DisableDevelopmentMode -Register "
            "\"$($_.InstallLocation)\\AppXManifest.xml\" }",
            timeout=150,
        )
        return {"ok": True, "message": out or "Re-registration requested."}

    return {"ok": False, "error": f"unknown remedy {remedy!r}"}
