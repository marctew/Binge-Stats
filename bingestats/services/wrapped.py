"""Wrapped: a year in review per user, and for the whole server (user_id 0).
SPEC.md section 13.

`compute()` builds the whole thing from plays. The worker keeps
`wrapped_cache` fresh (a daily `wrapped` job for the running year, and any
past year on first ask); the web process reads the cache and, on a miss,
computes on the spot without writing, so it never writes derived data.
"""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import date, datetime

from itsdangerous import BadSignature, URLSafeSerializer

from . import binges, settings, stats
from .binges import show_guids, show_key

SERVER = 0
FRESH_S = 86400
VERSION = 2   # 2: artwork and links follow re-added items (stats.current_items). Bump when the shape changes so old cache rows are ignored

# Comparisons for total watch time, biggest that fits first.
COMPARISONS = [
    (22.0, "trip", "trips", "London → Sydney"),
    (11.4, "run", "runs", "through the extended Lord of the Rings trilogy"),
    (1.5, "", "", "football matches"),
]


# -- range -------------------------------------------------------------------------------

def year_range(conn: sqlite3.Connection, year: int) -> stats.Range:
    tz = stats._tz(conn)
    return stats.Range(stats._midnight(date(year, 1, 1), tz), stats._midnight(date(year + 1, 1, 1), tz), tz, str(year))


def years(conn: sqlite3.Connection, user_id: int = SERVER) -> list[int]:
    """Years with plays, newest first."""
    tz = stats._tz(conn)
    where, params = ("WHERE user_id = ?", [user_id]) if user_id else ("", [])
    row = conn.execute(f"SELECT MIN(started_at), MAX(started_at) FROM plays {where}", params).fetchone()
    if not row or row[0] is None:
        return []
    first, last = datetime.fromtimestamp(row[0], tz).year, datetime.fromtimestamp(row[1], tz).year
    return list(range(last, first - 1, -1))


# -- compute -----------------------------------------------------------------------------

def comparison(hours: float) -> str | None:
    for size, one, many, what in COMPARISONS:
        n = hours / size
        if n >= 1:
            if not one:
                return f"that's {n:,.0f} {what}"
            shown = f"{n:,.1f}" if n < 10 else f"{n:,.0f}"
            return f"that's {shown} {one if shown == '1.0' else many} {what}"
    return None


def _finished_shows(conn: sqlite3.Connection, user_id: int | None, rng: stats.Range) -> list[dict]:
    """Shows whose last missing episode was first watched in this range (100 % completion)."""
    guids = show_guids(conn)
    where, params = ["p.media_type = 'episode'", "p.watched = 1", "p.guid IS NOT NULL", "p.started_at < ?"], [rng.end]
    if user_id:
        where.append("p.user_id = ?")
        params.append(user_id)
    first: dict[tuple[int, str], dict[str, int]] = defaultdict(dict)
    for r in conn.execute("SELECT p.user_id, p.guid, p.grandparent_guid, p.grandparent_rating_key, p.grandparent_title, "
                          f"MIN(p.started_at) AS t FROM plays p WHERE {' AND '.join(where)} "
                          "GROUP BY p.user_id, p.guid", params):
        key = show_key(r, guids)
        if key:
            first[(r["user_id"], key)][r["guid"]] = r["t"]
    if not first:
        return []
    shows = {r["guid"]: r for r in conn.execute(
        "SELECT rating_key, guid, title, thumb, year FROM media_items WHERE type = 'show' AND is_present = 1")}
    eps: dict[int, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT grandparent_rating_key, guid FROM media_items WHERE type = 'episode' AND is_present = 1 "
                          "AND guid IS NOT NULL AND COALESCE(season_index, 1) != 0"):
        eps[r[0]].add(r[1])
    out: dict[str, dict] = {}
    for (uid, key), seen in first.items():
        show = shows.get(key)
        if show is None or not eps.get(show["rating_key"]) or not eps[show["rating_key"]] <= seen.keys():
            continue
        done = max(seen[g] for g in eps[show["rating_key"]])
        if rng.start <= done < rng.end:
            cur = out.get(key)
            if cur is None or done < cur["finished_at"]:
                out[key] = {"rating_key": show["rating_key"], "title": show["title"], "thumb": show["thumb"],
                            "year": show["year"], "episodes": len(eps[show["rating_key"]]), "finished_at": done}
    return sorted(out.values(), key=lambda s: s["finished_at"])


def _play_brief(r) -> dict:
    title = r["grandparent_title"] if r["media_type"] == "episode" and r["grandparent_title"] else r["title"]
    sub = None
    if r["media_type"] == "episode" and r["season_index"] is not None and r["episode_index"] is not None:
        sub = f"S{r['season_index']:02d}E{r['episode_index']:02d} · {r['title']}"
    elif r["year"]:
        sub = str(r["year"])
    return {"title": title, "sub": sub, "thumb": r["thumb"], "at": r["started_at"], "media_type": r["media_type"]}


def compute(conn: sqlite3.Connection, user_id: int, year: int) -> dict:
    rng = year_range(conn, year)
    uid = user_id or None
    rows = conn.execute(
        "SELECT p.*, COALESCE(u.friendly_name, u.username) AS user_name FROM plays p "
        "LEFT JOIN users u ON u.id = p.user_id WHERE p.started_at >= ? AND p.started_at < ?"
        + (" AND p.user_id = ?" if uid else "") + " ORDER BY p.started_at, p.id",
        [rng.start, rng.end] + ([uid] if uid else [])).fetchall()
    heads = [r for r in rows if r["group_id"] is None]
    watch_s = sum(r["play_s"] or 0 for r in rows)
    hours = watch_s / 3600

    titles = {(r["grandparent_title"] if r["media_type"] == "episode" else r["title"]) or "" for r in heads} - {""}
    per_day: Counter = Counter()
    for r in rows:
        per_day[datetime.fromtimestamp(r["started_at"], rng.tz).date()] += r["play_s"] or 0
    busiest = per_day.most_common(1)[0] if per_day else None
    devices: Counter = Counter()
    for r in rows:
        devices[(r["player_title"] or "Unknown", r["product"] or "")] += r["play_s"] or 0
    dev = devices.most_common(1)[0][0] if devices else None
    direct = sum(1 for r in heads if r["decision"] == "direct_play")

    months = stats.by_period(conn, rng, dim="month", metric="watch_time", user_id=uid)
    month_hours = [round(sum(col), 1) for col in zip(*(s["data"] for s in months["series"]))] if months["series"] \
        else [0.0] * 12
    heat = stats.heatmap(conn, rng, metric="watch_time", user_id=uid)
    top_shows = stats.top(conn, rng, kind="shows", metric="watch_time", limit=5, user_id=uid)["items"]
    top_movies = stats.top(conn, rng, kind="movies", metric="watch_time", limit=5, user_id=uid)["items"]
    longest = binges.longest(conn, start=rng.start, end=rng.end, user_id=uid, limit=1)
    finished = _finished_shows(conn, uid, rng)

    # ranking among people with plays this year
    by_user = Counter()
    for r in conn.execute("SELECT user_id, SUM(play_s) FROM plays WHERE started_at >= ? AND started_at < ? "
                          "GROUP BY user_id", (rng.start, rng.end)):
        by_user[r[0]] = r[1] or 0
    ranked = [u for u, _s in by_user.most_common()]
    names = stats.user_names(conn)
    rank = None
    if uid and uid in by_user:
        pos = ranked.index(uid) + 1
        rank = {"position": pos, "of": len(ranked),
                "top_pct": max(1, math.ceil(pos / len(ranked) * 100))}
    people = [{"user_id": u, "name": names.get(u, str(u)), "hours": round(by_user[u] / 3600, 1)}
              for u in ranked[:5]] if not uid else []

    name = "Everyone" if not uid else names.get(uid, str(uid))
    return {
        "version": VERSION, "user_id": user_id, "year": year, "name": name,
        "plays": len(heads), "titles": len(titles), "watch_s": watch_s, "hours": round(hours, 1),
        "days_watched": len(per_day), "comparison": comparison(hours),
        "episodes": sum(1 for r in heads if r["media_type"] == "episode"),
        "movies": sum(1 for r in heads if r["media_type"] == "movie"),
        "top_shows": [{"title": s["name"], "thumb": s["thumb"], "rating_key": s["id"], "hours": round((s["play_s"] or 0) / 3600, 1),
                       "plays": s["plays"]} for s in top_shows],
        "top_movies": [{"title": m["name"], "thumb": m["thumb"], "rating_key": m["id"], "year": m.get("year"),
                        "hours": round((m["play_s"] or 0) / 3600, 1)} for m in top_movies],
        "binge": ({"show": longest[0]["show_title"], "episodes": longest[0]["episode_count"],
                   "hours": round(longest[0]["play_s"] / 3600, 1), "at": longest[0]["started_at"],
                   "thumb": longest[0]["show_thumb"], "user": longest[0]["user_name"]} if longest else None),
        "busiest_day": ({"date": busiest[0].isoformat(), "hours": round(busiest[1] / 3600, 1)} if busiest else None),
        "months": month_hours,
        "heatmap": {"data": heat["data"], "max": heat["max"]},
        "device": {"name": dev[0], "app": dev[1]} if dev else None,
        "direct_pct": round(direct / len(heads) * 100) if heads else 0,
        "first": _play_brief(heads[0]) if heads else None,
        "last": _play_brief(heads[-1]) if heads else None,
        "finished": finished,
        "rank": rank, "people": people,
    }


# -- cache -------------------------------------------------------------------------------

def cached(conn: sqlite3.Connection, user_id: int, year: int, now: float | None = None) -> dict | None:
    now = now or time.time()
    row = conn.execute("SELECT computed_at, data FROM wrapped_cache WHERE user_id = ? AND year = ?",
                       (user_id, year)).fetchone()
    if row is None:
        return None
    data = json.loads(row["data"])
    if data.get("version") != VERSION:
        return None
    running = year >= datetime.fromtimestamp(now, stats._tz(conn)).year
    if running and now - row["computed_at"] > FRESH_S:
        return None
    return data


def get(conn: sqlite3.Connection, user_id: int, year: int, now: float | None = None) -> dict:
    """The cached copy if fresh, else computed now (not stored: the worker stores)."""
    return cached(conn, user_id, year, now) or compute(conn, user_id, year)


def refresh(conn: sqlite3.Connection, year: int | None = None, progress=None, now: float | None = None) -> dict:
    """Worker job: recompute the running year (or `year`) for everyone with plays, plus the server."""
    now = now or time.time()
    year = year or datetime.fromtimestamp(now, stats._tz(conn)).year
    rng = year_range(conn, year)
    users = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM plays WHERE started_at >= ? AND started_at < ?",
                                        (rng.start, rng.end))]
    targets = [SERVER] + users
    for i, uid in enumerate(targets):
        data = compute(conn, uid, year)
        conn.execute("INSERT INTO wrapped_cache (user_id, year, computed_at, data) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(user_id, year) DO UPDATE SET computed_at = excluded.computed_at, data = excluded.data",
                     (uid, year, int(now), json.dumps(data)))
        if progress:
            progress(int((i + 1) / len(targets) * 100), f"{i + 1} of {len(targets)}")
    return {"year": year, "computed": len(targets)}


def clear(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM wrapped_cache")


# -- share links -------------------------------------------------------------------------

def _salt(conn: sqlite3.Connection, user_id: int) -> str | None:
    if user_id == SERVER:
        salt = settings.get(conn, "wrapped.server_salt")
        if not salt:
            salt = secrets.token_urlsafe(16)
            settings.set(conn, "wrapped.server_salt", salt)
        return salt
    row = conn.execute("SELECT share_salt FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["share_salt"] if row else None


def share_token(conn: sqlite3.Connection, secret_key: str, user_id: int, year: int) -> str:
    salt = _salt(conn, user_id)
    if salt is None:
        raise LookupError("No such user")
    return URLSafeSerializer(secret_key, salt=f"wrapped:{salt}").dumps({"u": user_id, "y": year})


def read_token(conn: sqlite3.Connection, secret_key: str, token: str) -> tuple[int, int] | None:
    """(user_id, year) for a valid link, None for a bad or revoked one."""
    try:
        _ok, payload = URLSafeSerializer(secret_key).loads_unsafe(token)
        uid, year = int(payload["u"]), int(payload["y"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None
    salt = _salt(conn, uid) if uid != SERVER else settings.get(conn, "wrapped.server_salt")
    if not salt:
        return None
    try:
        data = URLSafeSerializer(secret_key, salt=f"wrapped:{salt}").loads(token)
    except BadSignature:
        return None
    return (uid, year) if data == {"u": uid, "y": year} else None


def revoke(conn: sqlite3.Connection, user_id: int) -> None:
    """Every share link for this user (or the server) stops working."""
    if user_id == SERVER:
        settings.set(conn, "wrapped.server_salt", secrets.token_urlsafe(16))
    else:
        conn.execute("UPDATE users SET share_salt = ? WHERE id = ?", (secrets.token_urlsafe(16), user_id))


def image_paths(data: dict) -> set[str]:
    """Artwork a share link may load through the scoped image proxy."""
    paths = {x.get("thumb") for x in data.get("top_shows", []) + data.get("top_movies", []) + data.get("finished", [])}
    for k in ("binge", "first", "last"):
        if data.get(k):
            paths.add(data[k].get("thumb"))
    return {p for p in paths if p}


def month_labels() -> list[str]:
    return [date(2000, m, 1).strftime("%b") for m in range(1, 13)]


def weekday_labels() -> list[str]:
    return stats.WEEKDAYS


def busiest_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%A %-d %B")

