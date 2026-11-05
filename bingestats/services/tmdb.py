"""TMDB: the connection setting, its status, and which shows are due a read.

TMDB gives the production status the season outlook needs (cancelled vs ended,
in production), which Sonarr can't. The worker reads it (worker/tmdb_sync.py);
the web process only saves the key and shows the status.
"""
from __future__ import annotations

import json
import sqlite3
import time

from . import settings

PINNED_EVERY_S = 20 * 3600     # pinned shows: about daily (the job runs every RUN_EVERY_S)
OTHERS_EVERY_S = 7 * 86400     # everything else: weekly
RUN_EVERY_S = 6 * 3600


def configured(conn: sqlite3.Connection) -> bool:
    return bool(settings.get(conn, "tmdb.api_key"))


def client(conn: sqlite3.Connection):
    from ..tmdb.client import TmdbClient
    return TmdbClient(settings.get(conn, "tmdb.api_key")) if configured(conn) else None


def view(conn: sqlite3.Connection) -> dict:
    st = conn.execute("SELECT * FROM arr_status WHERE app = 'tmdb'").fetchone()
    st = dict(st) if st else {}
    return {"has_key": configured(conn), "ok": bool(st.get("ok")), "error": st.get("error"),
            "last_sync_at": st.get("last_sync_at"), "info": json.loads(st.get("info") or "{}"),
            "shows": conn.execute("SELECT COUNT(*) FROM tmdb_shows WHERE status IS NOT NULL").fetchone()[0],
            "known": len(known_shows(conn))}


def save(conn: sqlite3.Connection, form) -> bool:
    """A blank key keeps the saved one; 'clear_tmdb' removes it. Returns True if the key changed."""
    if form.get("clear_tmdb") == "on":
        settings.set(conn, "tmdb.api_key", "")
        conn.execute("DELETE FROM arr_status WHERE app = 'tmdb'")
        return True
    key = (form.get("tmdb.api_key") or "").strip()
    if not key:
        return False
    if len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("That doesn't look like a TMDB API key or read access token")
    settings.set(conn, "tmdb.api_key", key)
    return True


def known_shows(conn: sqlite3.Connection) -> dict[int, int | None]:
    """Every show we can ask TMDB about: tmdb id -> tvdb id, from Sonarr and the Plex library."""
    out: dict[int, int | None] = {}
    for r in conn.execute("SELECT tmdb_id, tvdb_id FROM media_items WHERE type = 'show' AND is_present = 1 "
                          "AND tmdb_id IS NOT NULL"):
        out[r[0]] = r[1]
    for r in conn.execute("SELECT tmdb_id, tvdb_id FROM sonarr_series WHERE tmdb_id IS NOT NULL"):
        out[r[0]] = r[1] or out.get(r[0])
    return out


def due(conn: sqlite3.Connection, now: float | None = None) -> list[tuple[int, int | None]]:
    """(tmdb id, tvdb id) to read now: pinned shows first, then never read, then the oldest reads.
    The first run reads everything; after that pinned shows about daily and the rest weekly."""
    now = now or time.time()
    known = known_shows(conn)
    fetched = {r[0]: r[1] for r in conn.execute("SELECT tmdb_id, fetched_at FROM tmdb_shows")}
    pinned = {r[0] for r in conn.execute("SELECT DISTINCT tvdb_id FROM pins")}
    out = []
    for tmdb_id, tvdb_id in known.items():
        at = fetched.get(tmdb_id)
        is_pinned = tvdb_id in pinned
        if at is None or now - at >= (PINNED_EVERY_S if is_pinned else OTHERS_EVERY_S):
            out.append((0 if is_pinned else 1 if at is None else 2, at or 0, tmdb_id, tvdb_id))
    out.sort()
    return [(t, v) for _p, _a, t, v in out]
