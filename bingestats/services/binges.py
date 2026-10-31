"""Binges (SPEC.md section 11), materialised in `binges`.

A run is a user's back-to-back episode plays of one show: in the user's
episode plays ordered by start, each play is the same show as the one before
and starts within `binge.gap_min` of it stopping. Movies in between don't
break a run; an episode of another show does. A run is a binge once it has
`binge.min_episodes` distinct episodes, so resumed parts and rewatches count
once.

`on_play()` (called by the poller when it records an episode play) walks back
from the new play and replaces the binge that ended with the previous one.
`rebuild()` recomputes the table from `plays` with the same `runs()` and is
used after imports and when the settings change. A test holds them equal.
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Iterable

from . import settings

COLS = ("id, user_id, started_at, stopped_at, play_s, guid, rating_key, grandparent_guid, "
        "grandparent_rating_key, grandparent_title")


def show_guids(conn: sqlite3.Connection) -> dict[int, str]:
    """Show rating key -> guid, for plays recorded without grandparent_guid."""
    return {r[0]: r[1] for r in conn.execute("SELECT rating_key, guid FROM media_items WHERE type = 'show' "
                                             "AND guid IS NOT NULL")}


def show_key(row, guids: dict[int, str]) -> str | None:
    """The show a play belongs to: its guid when known, else a rating-key or title stand-in."""
    if row["grandparent_guid"]:
        return row["grandparent_guid"]
    rk = row["grandparent_rating_key"]
    if rk and rk in guids:
        return guids[rk]
    if rk:
        return f"rk:{rk}"
    return f"title:{row['grandparent_title']}" if row["grandparent_title"] else None


def _episode_id(row) -> str:
    if row["guid"]:
        return row["guid"]
    return f"rk:{row['rating_key']}" if row["rating_key"] else f"play:{row['id']}"


def _config(conn: sqlite3.Connection) -> tuple[int, int]:
    return int(settings.get(conn, "binge.gap_min")) * 60, int(settings.get(conn, "binge.min_episodes"))


def runs(rows: Iterable, gap_s: int, guids: dict[int, str]) -> list[list]:
    """Split one user's episode plays (ordered by start) into runs."""
    out: list[list] = []
    cur: list = []
    cur_key = None
    for r in rows:
        key = show_key(r, guids)
        if cur and key is not None and key == cur_key and r["started_at"] - cur[-1]["stopped_at"] <= gap_s:
            cur.append(r)
            continue
        if cur:
            out.append(cur)
        cur, cur_key = [r], key
    if cur:
        out.append(cur)
    return out


def _binge(run: list, guids: dict[int, str]) -> dict:
    last_title = next((r["grandparent_title"] for r in reversed(run) if r["grandparent_title"]), None)
    return {"user_id": run[0]["user_id"], "grandparent_guid": show_key(run[0], guids), "show_title": last_title,
            "started_at": run[0]["started_at"], "ended_at": max(r["stopped_at"] for r in run),
            "episode_count": len({_episode_id(r) for r in run}), "play_s": sum(r["play_s"] or 0 for r in run),
            "first_play_id": run[0]["id"], "last_play_id": run[-1]["id"]}


def _insert(conn: sqlite3.Connection, b: dict) -> None:
    conn.execute("INSERT INTO binges (user_id, grandparent_guid, show_title, started_at, ended_at, episode_count, "
                 "play_s, first_play_id, last_play_id) VALUES (:user_id, :grandparent_guid, :show_title, "
                 ":started_at, :ended_at, :episode_count, :play_s, :first_play_id, :last_play_id)", b)


def rebuild(conn: sqlite3.Connection, progress: Callable[[int, str], None] | None = None) -> dict:
    """Recompute every binge from plays. The caller holds the transaction."""
    gap_s, min_eps = _config(conn)
    guids = show_guids(conn)
    conn.execute("DELETE FROM binges")
    users = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM plays WHERE media_type = 'episode'")]
    count = 0
    for i, uid in enumerate(users):
        rows = conn.execute(f"SELECT {COLS} FROM plays WHERE user_id = ? AND media_type = 'episode' "
                            "ORDER BY started_at, id", (uid,)).fetchall()
        for run in runs(rows, gap_s, guids):
            b = _binge(run, guids)
            if b["episode_count"] >= min_eps and b["grandparent_guid"]:
                _insert(conn, b)
                count += 1
        if progress and users:
            progress(int((i + 1) / len(users) * 100), f"{i + 1} of {len(users)} people")
    return {"binges": count, "users": len(users)}


def on_play(conn: sqlite3.Connection, play_id: int) -> dict | None:
    """Extend or create the binge ending at this play. Returns the binge, if any."""
    row = conn.execute(f"SELECT {COLS}, media_type FROM plays WHERE id = ?", (play_id,)).fetchone()
    if row is None or row["media_type"] != "episode":
        return None
    gap_s, min_eps = _config(conn)
    guids = show_guids(conn)
    key = show_key(row, guids)
    if key is None:
        return None
    chain = [row]
    prev_rows = conn.execute(
        f"SELECT {COLS} FROM plays WHERE user_id = ? AND media_type = 'episode' AND id != ? "
        "AND (started_at < ? OR (started_at = ? AND id < ?)) ORDER BY started_at DESC, id DESC",
        (row["user_id"], row["id"], row["started_at"], row["started_at"], row["id"]))
    for prev in prev_rows:
        if show_key(prev, guids) != key or chain[-1]["started_at"] - prev["stopped_at"] > gap_s:
            break
        chain.append(prev)
    run = list(reversed(chain))
    b = _binge(run, guids)
    if b["episode_count"] < min_eps:
        return None
    ids = [r["id"] for r in run]
    conn.execute(f"DELETE FROM binges WHERE user_id = ? AND last_play_id IN ({','.join('?' * len(ids))})",
                 [row["user_id"], *ids])
    _insert(conn, b)
    return b


# -- queries ----------------------------------------------------------------------------

_SELECT = """SELECT b.*, COALESCE(u.friendly_name, u.username) AS user_name,
                    s.rating_key AS show_rating_key, s.thumb AS show_thumb
             FROM binges b LEFT JOIN users u ON u.id = b.user_id
             LEFT JOIN media_items s ON s.guid = b.grandparent_guid AND s.type = 'show'"""


def _where(start: int | None, end: int | None, user_id: int | None) -> tuple[str, list]:
    where, params = [], []
    if start is not None:
        where.append("b.started_at >= ?")
        params.append(start)
    if end is not None:
        where.append("b.started_at < ?")
        params.append(end)
    if user_id is not None:
        where.append("b.user_id = ?")
        params.append(user_id)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def longest(conn: sqlite3.Connection, *, start: int | None = None, end: int | None = None,
            user_id: int | None = None, limit: int = 10) -> list[dict]:
    w, p = _where(start, end, user_id)
    return [dict(r) for r in conn.execute(
        f"{_SELECT}{w} GROUP BY b.id ORDER BY b.episode_count DESC, b.play_s DESC, b.started_at DESC LIMIT ?",
        p + [limit])]


def by_show(conn: sqlite3.Connection, *, start: int | None = None, end: int | None = None,
            limit: int = 10) -> list[dict]:
    w, p = _where(start, end, None)
    return [dict(r) for r in conn.execute(
        f"""SELECT b.grandparent_guid, MAX(b.show_title) AS show_title, COUNT(*) AS binges,
                   SUM(b.episode_count) AS episodes, SUM(b.play_s) AS play_s, MAX(b.episode_count) AS longest,
                   COUNT(DISTINCT b.user_id) AS people,
                   (SELECT s.rating_key FROM media_items s WHERE s.guid = b.grandparent_guid AND s.type = 'show'
                    LIMIT 1) AS show_rating_key,
                   (SELECT s.thumb FROM media_items s WHERE s.guid = b.grandparent_guid AND s.type = 'show'
                    LIMIT 1) AS show_thumb
            FROM binges b{w} GROUP BY b.grandparent_guid ORDER BY binges DESC, episodes DESC LIMIT ?""",
        p + [limit])]


def leaderboard(conn: sqlite3.Connection, *, start: int | None = None, end: int | None = None,
                limit: int = 10) -> list[dict]:
    w, p = _where(start, end, None)
    return [dict(r) for r in conn.execute(
        f"""SELECT b.user_id, COALESCE(u.friendly_name, u.username) AS user_name, COUNT(*) AS binges,
                   SUM(b.episode_count) AS episodes, SUM(b.play_s) AS play_s, MAX(b.episode_count) AS longest
            FROM binges b LEFT JOIN users u ON u.id = b.user_id{w}
            GROUP BY b.user_id ORDER BY binges DESC, episodes DESC LIMIT ?""", p + [limit])]


def totals(conn: sqlite3.Connection, *, start: int | None = None, end: int | None = None,
           user_id: int | None = None) -> dict:
    w, p = _where(start, end, user_id)
    r = conn.execute(f"SELECT COUNT(*) AS binges, COALESCE(SUM(episode_count), 0) AS episodes, "
                     f"COALESCE(SUM(play_s), 0) AS play_s, COALESCE(MAX(episode_count), 0) AS longest, "
                     f"COUNT(DISTINCT grandparent_guid) AS shows FROM binges b{w}", p).fetchone()
    return dict(r)
