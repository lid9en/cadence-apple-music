"""Local listening history and statistics, in SQLite.

Nothing leaves the machine. A play is recorded once it clears either the
absolute threshold (default 60s) or the fractional one (default half the
track) -- the same rule scrobblers have used for years, which keeps
skipped tracks out of your stats.

That rule also makes this the honest place to notice the Apple Music
instant-skip bug: tracks that advance in under a second are counted as
skips and never recorded as plays.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import config_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    track_key  TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    artist     TEXT    NOT NULL DEFAULT '',
    album      TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT '',
    duration   REAL    NOT NULL DEFAULT 0,
    listened   REAL    NOT NULL DEFAULT 0,
    ts         INTEGER NOT NULL,
    day        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plays_ts     ON plays(ts);
CREATE INDEX IF NOT EXISTS idx_plays_artist ON plays(artist);
CREATE INDEX IF NOT EXISTS idx_plays_key    ON plays(track_key);
CREATE INDEX IF NOT EXISTS idx_plays_day    ON plays(day);

CREATE TABLE IF NOT EXISTS skips (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_key TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    artist    TEXT NOT NULL DEFAULT '',
    source    TEXT NOT NULL DEFAULT '',
    listened  REAL NOT NULL DEFAULT 0,
    ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skips_ts ON skips(ts);

CREATE TABLE IF NOT EXISTS meta_cache (
    track_key  TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS lyrics_cache (
    track_key  TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);
"""


def db_path() -> Path:
    return config_dir() / "history.db"


class History:
    def __init__(self, path: Path | None = None):
        self._path = path or db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- writes --------------------------------------------------------

    def record_play(self, st: dict, listened: float) -> None:
        now = int(time.time())
        day = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            self._conn.execute(
                "INSERT INTO plays (track_key,title,artist,album,source,duration,"
                "listened,ts,day) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    st.get("track_key", ""),
                    st.get("title", ""),
                    st.get("artist", ""),
                    st.get("album", ""),
                    st.get("source_label", ""),
                    float(st.get("duration") or 0),
                    float(listened),
                    now,
                    day,
                ),
            )
            self._conn.commit()

    def record_skip(self, st: dict, listened: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO skips (track_key,title,artist,source,listened,ts) "
                "VALUES (?,?,?,?,?,?)",
                (
                    st.get("track_key", ""),
                    st.get("title", ""),
                    st.get("artist", ""),
                    st.get("source_label", ""),
                    float(listened),
                    int(time.time()),
                ),
            )
            self._conn.commit()

    # ---- caches --------------------------------------------------------

    def cache_get(self, table: str, key: str, max_age_days: int) -> str | None:
        if table not in ("meta_cache", "lyrics_cache"):
            raise ValueError(table)
        cutoff = int(time.time()) - max_age_days * 86400
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload FROM {table} WHERE track_key=? AND fetched_at>=?",
                (key, cutoff),
            ).fetchone()
        return row["payload"] if row else None

    def cache_put(self, table: str, key: str, payload: str) -> None:
        if table not in ("meta_cache", "lyrics_cache"):
            raise ValueError(table)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {table} (track_key,payload,fetched_at) VALUES (?,?,?) "
                "ON CONFLICT(track_key) DO UPDATE SET payload=excluded.payload, "
                "fetched_at=excluded.fetched_at",
                (key, payload, int(time.time())),
            )
            self._conn.commit()

    # ---- reads ---------------------------------------------------------

    def _rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def stats(self, days: int = 0, limit: int = 12) -> dict[str, Any]:
        """days=0 means all time."""
        where, args = "", []
        if days > 0:
            where = "WHERE ts >= ?"
            args = [int(time.time()) - days * 86400]

        totals = self._rows(
            f"SELECT COUNT(*) AS plays, COALESCE(SUM(listened),0) AS seconds, "
            f"COUNT(DISTINCT artist) AS artists, COUNT(DISTINCT track_key) AS tracks "
            f"FROM plays {where}",
            args,
        )[0]

        top_artists = self._rows(
            f"SELECT artist, COUNT(*) AS plays, COALESCE(SUM(listened),0) AS seconds "
            f"FROM plays {where} {'AND' if where else 'WHERE'} artist != '' "
            f"GROUP BY artist ORDER BY plays DESC, seconds DESC LIMIT ?",
            [*args, limit],
        )
        top_tracks = self._rows(
            f"SELECT title, artist, COUNT(*) AS plays, "
            f"COALESCE(SUM(listened),0) AS seconds FROM plays {where} "
            f"{'AND' if where else 'WHERE'} title != '' "
            f"GROUP BY track_key ORDER BY plays DESC, seconds DESC LIMIT ?",
            [*args, limit],
        )
        top_albums = self._rows(
            f"SELECT album, artist, COUNT(*) AS plays FROM plays {where} "
            f"{'AND' if where else 'WHERE'} album != '' "
            f"GROUP BY album, artist ORDER BY plays DESC LIMIT ?",
            [*args, limit],
        )
        recent = self._rows(
            "SELECT title, artist, album, ts, listened FROM plays "
            "ORDER BY ts DESC LIMIT 40"
        )

        skip_where, skip_args = ("WHERE ts >= ?", args) if days > 0 else ("", [])
        skips = self._rows(
            f"SELECT COUNT(*) AS n FROM skips {skip_where}", skip_args
        )[0]["n"]

        return {
            "totals": {
                "plays": totals["plays"],
                "minutes": round((totals["seconds"] or 0) / 60),
                "hours": round((totals["seconds"] or 0) / 3600, 1),
                "artists": totals["artists"],
                "tracks": totals["tracks"],
                "skips": skips,
            },
            "top_artists": top_artists,
            "top_tracks": top_tracks,
            "top_albums": top_albums,
            "recent": recent,
            "heatmap": self.heatmap(),
            "by_hour": self.by_hour(days),
        }

    def heatmap(self, weeks: int = 26) -> list[dict]:
        start = (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        rows = self._rows(
            "SELECT day, COUNT(*) AS plays FROM plays WHERE day >= ? "
            "GROUP BY day ORDER BY day",
            (start,),
        )
        return rows

    def by_hour(self, days: int = 0) -> list[int]:
        where, args = "", []
        if days > 0:
            where = "WHERE ts >= ?"
            args = [int(time.time()) - days * 86400]
        rows = self._rows(
            f"SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) "
            f"AS h, COUNT(*) AS n FROM plays {where} GROUP BY h",
            args,
        )
        buckets = [0] * 24
        for r in rows:
            if r["h"] is not None:
                buckets[int(r["h"])] = r["n"]
        return buckets

    def recent_skip_rate(self, seconds: int = 300) -> dict[str, Any]:
        """Used by the skip-storm detector on the Fix screen."""
        cutoff = int(time.time()) - seconds
        n_skips = self._rows(
            "SELECT COUNT(*) AS n FROM skips WHERE ts >= ?", (cutoff,)
        )[0]["n"]
        n_plays = self._rows(
            "SELECT COUNT(*) AS n FROM plays WHERE ts >= ?", (cutoff,)
        )[0]["n"]
        med = self._rows(
            "SELECT listened FROM skips WHERE ts >= ? ORDER BY listened", (cutoff,)
        )
        median = med[len(med) // 2]["listened"] if med else 0.0
        return {
            "window_seconds": seconds,
            "skips": n_skips,
            "plays": n_plays,
            "median_skip_seconds": round(median, 2),
            "storm": n_skips >= 8 and median < 3.0,
        }

    def purge(self) -> None:
        with self._lock:
            self._conn.executescript(
                "DELETE FROM plays; DELETE FROM skips; VACUUM;"
            )
            self._conn.commit()


class PlayTracker:
    """Turns a stream of state snapshots into play/skip records."""

    def __init__(self, history: History, settings):
        self.history = history
        self.settings = settings
        self._key: str | None = None
        self._state: dict = {}
        self._listened = 0.0
        self._last_tick: float | None = None
        self._recorded = False

    def tick(self, st: dict) -> None:
        cfg = self.settings.section("history")
        if not cfg.get("enabled", True):
            return
        if st.get("source_label") in (cfg.get("ignore_sources") or []):
            return

        key = st.get("track_key") or None
        now = time.monotonic()

        if key != self._key:
            self._flush()
            self._key = key
            self._state = dict(st)
            self._listened = 0.0
            self._recorded = False
            self._last_tick = now if st.get("playing") else None
            return

        if st.get("playing"):
            if self._last_tick is not None:
                delta = now - self._last_tick
                # Ignore absurd gaps (sleep/hibernate) rather than inflating.
                if 0 < delta < 10:
                    self._listened += delta
            self._last_tick = now
            self._state = dict(st)
        else:
            self._last_tick = None

        if not self._recorded and self._qualifies(cfg):
            self.history.record_play(self._state, self._listened)
            self._recorded = True

    def _qualifies(self, cfg) -> bool:
        dur = float(self._state.get("duration") or 0)
        if self._listened >= float(cfg.get("min_seconds", 60)):
            return True
        if dur > 0 and self._listened / dur >= float(cfg.get("min_fraction", 0.5)):
            return True
        return False

    def _flush(self) -> None:
        if not self._key or not self._state.get("title"):
            return
        if self._recorded:
            return
        # Never qualified: that is a skip.
        self.history.record_skip(self._state, self._listened)
