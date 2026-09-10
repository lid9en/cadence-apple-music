"""Cadence's own playlists, and the driver that plays them.

SMTC can report and steer the *current* track but has no notion of a
library, and no "play item X" verb. So Cadence keeps its own playlists and
plays them by handing each track's Apple Music URL to a music client,
which starts that track; from then on the normal SMTC path takes over for
transport control and now-playing.

Which client receives the handoff must be chosen explicitly. The `itmss`
association is frequently owned by whichever Apple client was installed
last -- on a machine with the Store app, legacy iTunes and a third-party
Electron wrapper, "the system default" is not a useful answer.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .history import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    note       TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    artist      TEXT    NOT NULL DEFAULT '',
    album       TEXT    NOT NULL DEFAULT '',
    apple_url   TEXT    NOT NULL DEFAULT '',
    artwork_url TEXT    NOT NULL DEFAULT '',
    duration    REAL    NOT NULL DEFAULT 0,
    store_id    INTEGER,
    added_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pt_playlist ON playlist_tracks(playlist_id, position);
"""


# --------------------------------------------------------------------------
# Handoff targets
# --------------------------------------------------------------------------

def _reg_command(root: str, scheme: str) -> str:
    """The registered shell command for a URL scheme, or ''."""
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-ItemProperty '{root}\\{scheme}\\shell\\open\\command'"
             " -ErrorAction SilentlyContinue).'(default)'"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _exe_from_command(cmd: str) -> str:
    """Pull the executable path out of a registered shell command string."""
    cmd = (cmd or "").strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        return cmd[1:end] if end > 1 else ""
    return cmd.split(" ")[0] if cmd else ""


def detect_targets() -> list[dict]:
    """Which music clients on this machine can accept a track URL."""
    targets: list[dict] = [{
        "id": "default",
        "label": "System default for itmss:// links",
        "exe": "",
        "available": True,
        "note": "Whichever app currently owns the protocol association.",
    }]

    seen: set[str] = set()
    for root, label in (("HKCU:\\SOFTWARE\\Classes", "user"),
                        ("HKLM:\\SOFTWARE\\Classes", "machine")):
        exe = _exe_from_command(_reg_command(root, "itmss"))
        if exe and os.path.isfile(exe) and exe.lower() not in seen:
            seen.add(exe.lower())
            name = os.path.basename(exe)
            targets.append({
                "id": f"exe:{exe}",
                "label": f"{name}  ({label} association)",
                "exe": exe,
                "available": True,
                "note": exe,
            })

    store = (
        "Get-AppxPackage AppleInc.AppleMusicWin | Select-Object -First 1"
        " -ExpandProperty PackageFamilyName"
    )
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", store],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        family = (out.stdout or "").strip()
    except Exception:
        family = ""
    if family:
        targets.append({
            "id": f"appx:{family}",
            "label": "Apple Music (Microsoft Store app)",
            "exe": "",
            "available": True,
            "note": "Launched through its app activation entry.",
        })
    return targets


def to_itmss(url: str) -> str:
    """Apple Music web links open in a client under the itmss scheme."""
    url = (url or "").strip()
    if url.startswith("https://"):
        return "itmss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "itmss://" + url[len("http://"):]
    return url


def launch_track(apple_url: str, target_id: str = "default") -> dict:
    """Hand one track to a music client. Never raises."""
    if not apple_url:
        return {"ok": False, "error": "This track has no Apple Music link."}
    uri = to_itmss(apple_url)

    try:
        if target_id.startswith("exe:"):
            exe = target_id[4:]
            if not os.path.isfile(exe):
                return {"ok": False, "error": f"{exe} is no longer installed."}
            subprocess.Popen([exe, uri], close_fds=True)
        elif target_id.startswith("appx:"):
            family = target_id[5:]
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{family}!App"],
                close_fds=True,
            )
            time.sleep(1.2)  # let it come up before it is handed the URL
            os.startfile(uri)
        else:
            os.startfile(uri)
        return {"ok": True, "uri": uri, "target": target_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

class Playlists:
    def __init__(self, path=None):
        self._path = path or db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # ---- playlists -----------------------------------------------------

    def all(self) -> list[dict]:
        return self._rows(
            "SELECT p.*, COUNT(t.id) AS track_count, "
            "COALESCE(SUM(t.duration), 0) AS seconds "
            "FROM playlists p LEFT JOIN playlist_tracks t "
            "ON t.playlist_id = p.id GROUP BY p.id ORDER BY p.updated_at DESC"
        )

    def create(self, name: str, note: str = "") -> dict:
        name = (name or "").strip() or "New playlist"
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO playlists (name, note, created_at, updated_at) "
                "VALUES (?,?,?,?)", (name, note, now, now))
            self._conn.commit()
            new_id = cur.lastrowid
        return {"ok": True, "id": new_id, "name": name}

    def rename(self, playlist_id: int, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "A playlist needs a name."}
        with self._lock:
            self._conn.execute(
                "UPDATE playlists SET name=?, updated_at=? WHERE id=?",
                (name, int(time.time()), int(playlist_id)))
            self._conn.commit()
        return {"ok": True}

    def delete(self, playlist_id: int) -> dict:
        with self._lock:
            self._conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?",
                               (int(playlist_id),))
            self._conn.execute("DELETE FROM playlists WHERE id=?",
                               (int(playlist_id),))
            self._conn.commit()
        return {"ok": True}

    # ---- tracks --------------------------------------------------------

    def tracks(self, playlist_id: int) -> list[dict]:
        return self._rows(
            "SELECT * FROM playlist_tracks WHERE playlist_id=? "
            "ORDER BY position, id", (int(playlist_id),))

    def _touch(self, playlist_id: int) -> None:
        self._conn.execute("UPDATE playlists SET updated_at=? WHERE id=?",
                           (int(time.time()), int(playlist_id)))

    def add_track(self, playlist_id: int, track: dict) -> dict:
        if not track.get("apple_url"):
            return {"ok": False,
                    "error": "Without an Apple Music link Cadence cannot play "
                             "this track, so it is not worth adding."}
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS pos "
                "FROM playlist_tracks WHERE playlist_id=?",
                (int(playlist_id),)).fetchone()
            self._conn.execute(
                "INSERT INTO playlist_tracks (playlist_id, position, title, "
                "artist, album, apple_url, artwork_url, duration, store_id, "
                "added_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (int(playlist_id), row["pos"], track.get("title", ""),
                 track.get("artist", ""), track.get("album", ""),
                 track.get("apple_url", ""), track.get("artwork_url", ""),
                 float(track.get("duration") or 0), track.get("store_id"),
                 int(time.time())))
            self._touch(playlist_id)
            self._conn.commit()
        return {"ok": True}

    def remove_track(self, track_row_id: int) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT playlist_id FROM playlist_tracks WHERE id=?",
                (int(track_row_id),)).fetchone()
            if not row:
                return {"ok": False, "error": "That track is already gone."}
            self._conn.execute("DELETE FROM playlist_tracks WHERE id=?",
                               (int(track_row_id),))
            self._touch(row["playlist_id"])
            self._conn.commit()
            self._renumber(row["playlist_id"])
        return {"ok": True}

    def _renumber(self, playlist_id: int) -> None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM playlist_tracks WHERE playlist_id=? "
                "ORDER BY position, id", (int(playlist_id),)).fetchall()
            for i, r in enumerate(rows):
                self._conn.execute(
                    "UPDATE playlist_tracks SET position=? WHERE id=?",
                    (i, r["id"]))
            self._conn.commit()

    def move_track(self, playlist_id: int, from_index: int, to_index: int) -> dict:
        rows = self.tracks(playlist_id)
        if not rows:
            return {"ok": False, "error": "Playlist is empty."}
        n = len(rows)
        i = max(0, min(n - 1, int(from_index)))
        j = max(0, min(n - 1, int(to_index)))
        if i == j:
            return {"ok": True}
        order = [r["id"] for r in rows]
        order.insert(j, order.pop(i))
        with self._lock:
            for pos, row_id in enumerate(order):
                self._conn.execute(
                    "UPDATE playlist_tracks SET position=? WHERE id=?",
                    (pos, row_id))
            self._touch(playlist_id)
            self._conn.commit()
        return {"ok": True}

    def add_from_history(self, playlist_id: int, rows: list[dict]) -> dict:
        """Bulk add, used by 'build from my top tracks'."""
        added = 0
        for r in rows:
            if self.add_track(playlist_id, r).get("ok"):
                added += 1
        return {"ok": True, "added": added}


# --------------------------------------------------------------------------
# Playback driver
# --------------------------------------------------------------------------

def should_advance(state: dict, queue: dict) -> bool:
    """Decide whether to hand the next queued track to the music client.

    Called roughly four times a second while a Cadence playlist is playing.
    Returning True launches `queue["tracks"][queue["index"] + 1]`.

    Args:
        state: the live SMTC snapshot -- keys include "playing" (bool),
            "status" (e.g. "Playing", "Paused", "Stopped"), "position" and
            "duration" (seconds, duration may be 0 if unknown), "title",
            "artist", and "track_key" (stable id for the current track).
        queue: Cadence's own view, with
            "index" (int, position of the track Cadence last launched),
            "tracks" (list of track dicts, each with "title"/"artist"),
            "launched_at" (float, time.monotonic() when it was handed off),
            "launched_key" (str, the SMTC track_key seen after handoff, or
                "" if the handoff has not been observed taking effect yet),
            "advanced_at" (float, time.monotonic() of the last advance).

    Returns:
        True to launch the next track now, False to keep waiting.
    """
    # TODO(human): implement the advance policy.
    raise NotImplementedError


class QueueDriver:
    """Plays a Cadence playlist by handing tracks to a music client."""

    def __init__(self, settings, playlists: Playlists):
        self.settings = settings
        self.playlists = playlists
        self._lock = threading.RLock()
        self._queue: dict[str, Any] = self._empty()
        self.on_change: Callable[[dict], None] | None = None

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"playlist_id": None, "name": "", "tracks": [], "index": -1,
                "active": False, "launched_at": 0.0, "launched_key": "",
                "advanced_at": 0.0, "last_error": ""}

    def status(self) -> dict:
        with self._lock:
            q = dict(self._queue)
        q["track_count"] = len(q["tracks"])
        q["current"] = (q["tracks"][q["index"]]
                        if 0 <= q["index"] < len(q["tracks"]) else None)
        return q

    def _target(self) -> str:
        return self.settings.section("playlists").get("launch_target", "default")

    def play(self, playlist_id: int, index: int = 0) -> dict:
        tracks = self.playlists.tracks(playlist_id)
        if not tracks:
            return {"ok": False, "error": "That playlist has no tracks yet."}
        meta = next((p for p in self.playlists.all()
                     if p["id"] == int(playlist_id)), {})
        with self._lock:
            self._queue = self._empty()
            self._queue.update({
                "playlist_id": int(playlist_id),
                "name": meta.get("name", ""),
                "tracks": tracks,
                "active": True,
            })
        return self._launch(max(0, min(len(tracks) - 1, int(index))))

    def _launch(self, index: int) -> dict:
        with self._lock:
            tracks = self._queue["tracks"]
            if not (0 <= index < len(tracks)):
                self._queue["active"] = False
                self._notify()
                return {"ok": False, "error": "End of playlist."}
            track = tracks[index]

        result = launch_track(track.get("apple_url", ""), self._target())
        with self._lock:
            self._queue["index"] = index
            self._queue["launched_at"] = time.monotonic()
            self._queue["launched_key"] = ""
            self._queue["advanced_at"] = time.monotonic()
            self._queue["last_error"] = "" if result.get("ok") else result.get(
                "error", "")
        self._notify()
        return result

    def next(self) -> dict:
        with self._lock:
            idx = self._queue["index"]
        return self._launch(idx + 1)

    def previous(self) -> dict:
        with self._lock:
            idx = self._queue["index"]
        return self._launch(max(0, idx - 1))

    def stop(self) -> dict:
        with self._lock:
            self._queue["active"] = False
        self._notify()
        return {"ok": True}

    def tick(self, state: dict) -> None:
        """Fed from the media engine; advances the queue when a track ends."""
        with self._lock:
            if not self._queue["active"] or not self._queue["tracks"]:
                return
            # Record the SMTC identity of whatever the handoff produced, so
            # the policy can tell "our track" from one the user picked.
            if not self._queue["launched_key"] and state.get("track_key"):
                if time.monotonic() - self._queue["launched_at"] > 1.0:
                    self._queue["launched_key"] = state["track_key"]
            queue = dict(self._queue)

        try:
            advance = should_advance(state, queue)
        except NotImplementedError:
            return
        except Exception:
            return

        if advance:
            self.next()

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.status())
            except Exception:
                pass
