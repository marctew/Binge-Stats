"""Series completion and abandoned shows (SPEC.md section 11).

Completion, per user per show: distinct watched episodes ÷ episodes present
in the library. Episodes are matched by guid, specials (season 0) are left
out of both sides, and only episodes still in the library count as watched,
so deleting a watched season can't push a show past 100 %. Shows no longer in
the library have no completion.

Abandoned: at least one watched episode, below 100 %, no play of the show for
`abandoned.days`, and the show still in the library.

Computed on query and cached for 10 minutes per user (per process).
"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict

from . import settings
from .binges import show_guids, show_key

CACHE_S = 600
_cache: dict[tuple, tuple[float, object]] = {}


def clear_cache() -> None:
    _cache.clear()


def _cached(key: tuple, now: float, fn):
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def _shows_by_guid(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["guid"]: r for r in conn.execute(
        "SELECT rating_key, guid, title, thumb, year, library_id FROM media_items "
        "WHERE type = 'show' AND is_present = 1 AND guid IS NOT NULL")}


def _episodes(conn: sqlite3.Connection, show_rks: list[int]) -> dict[int, set[str]]:
    out: dict[int, set[str]] = defaultdict(set)
    for i in range(0, len(show_rks), 500):
        chunk = show_rks[i:i + 500]
        for r in conn.execute(
                f"SELECT grandparent_rating_key, guid FROM media_items WHERE type = 'episode' AND is_present = 1 "
                f"AND guid IS NOT NULL AND COALESCE(season_index, 1) != 0 "
                f"AND grandparent_rating_key IN ({','.join('?' * len(chunk))})", chunk):
            out[r[0]].add(r[1])
    return out


def _compute(conn: sqlite3.Connection, user_id: int | None, show_rating_key: int | None) -> list[dict]:
    guids = show_guids(conn)
    shows = _shows_by_guid(conn)
    where, params = ["p.media_type = 'episode'"], []
    if user_id is not None:
        where.append("p.user_id = ?")
        params.append(user_id)
    rows = conn.execute(
        "SELECT p.user_id, p.guid, p.watched, p.started_at, p.grandparent_guid, p.grandparent_rating_key, "
        "p.grandparent_title, COALESCE(u.friendly_name, u.username) AS user_name "
        f"FROM plays p LEFT JOIN users u ON u.id = p.user_id WHERE {' AND '.join(where)}", params).fetchall()
    acc: dict[tuple[int, str], dict] = {}
    for r in rows:
        key = show_key(r, guids)
        show = shows.get(key)
        if show is None or (show_rating_key is not None and show["rating_key"] != show_rating_key):
            continue
        a = acc.setdefault((r["user_id"], key), {
            "user_id": r["user_id"], "user_name": r["user_name"], "rating_key": show["rating_key"],
            "title": show["title"], "thumb": show["thumb"], "year": show["year"], "watched_guids": set(),
            "played_guids": set(), "last_played": 0})
        a["last_played"] = max(a["last_played"], r["started_at"])
        if r["guid"]:
            a["played_guids"].add(r["guid"])
            if r["watched"]:
                a["watched_guids"].add(r["guid"])
    eps = _episodes(conn, sorted({a["rating_key"] for a in acc.values()}))
    out = []
    for a in acc.values():
        present = eps.get(a["rating_key"], set())
        if not present:
            continue
        watched = len(a.pop("watched_guids") & present)
        a.pop("played_guids")
        a.update(watched=watched, total=len(present), pct=min(100, round(watched / len(present) * 100, 1)))
        out.append(a)
    out.sort(key=lambda a: -a["last_played"])
    return out


def for_user(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> list[dict]:
    """Every show this user has played that's still in the library, most recent first."""
    now = now or time.time()
    return _cached(("user", user_id), now, lambda: _compute(conn, user_id, None))


def for_show(conn: sqlite3.Connection, show_rating_key: int, now: float | None = None) -> list[dict]:
    """Everyone who has played this show, furthest through first."""
    now = now or time.time()
    rows = _cached(("show", show_rating_key), now, lambda: _compute(conn, None, show_rating_key))
    return sorted(rows, key=lambda a: (-a["pct"], -a["last_played"]))


def abandoned(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> list[dict]:
    now = now or time.time()
    cutoff = now - int(settings.get(conn, "abandoned.days")) * 86400
    return [a for a in for_user(conn, user_id, now)
            if a["watched"] >= 1 and a["pct"] < 100 and a["last_played"] < cutoff]


def summary(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> dict:
    """In progress, finished and abandoned, for the user page."""
    now = now or time.time()
    rows = for_user(conn, user_id, now)
    gone = {a["rating_key"] for a in abandoned(conn, user_id, now)}
    return {
        "in_progress": [a for a in rows if 0 < a["pct"] < 100 and a["rating_key"] not in gone],
        "finished": sorted([a for a in rows if a["pct"] >= 100], key=lambda a: -a["last_played"]),
        "abandoned": [a for a in rows if a["rating_key"] in gone],
    }
