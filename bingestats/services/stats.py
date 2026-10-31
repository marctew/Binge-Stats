"""Stats for the dashboard, graphs and the REST API.

Plays are bucketed in Python, not SQL, because buckets are local days, weeks
and hours (ui.timezone) and SQLite can't do DST-correct timezone maths. A
homelab's history is tens of thousands of rows, which is quick to walk.

Counting rules, used everywhere:
- "plays" counts a resumed sequence once (group heads only),
- "watch time" sums every part's play_s, attributed to when that part started.
"""
from __future__ import annotations

import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from ..transcode import RESOLUTION_RANK
from . import settings

METRICS = ("plays", "watch_time")
GROUPS = ("day", "week", "month")
SPLITS = (None, "media_type", "user", "decision")
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MEDIA_LABELS = {"movie": "Films", "episode": "TV", "track": "Music"}
DECISION_LABELS = {"direct_play": "Direct Play", "direct_stream": "Direct Stream", "transcode": "Transcode"}
PRESETS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
MAX_SERIES = 8

# Colour follows the entity, not its rank in a chart: each known series gets a
# fixed palette slot (see static/js/charts.js). Users get theirs from all-time
# ranking, so a user keeps their colour whatever the range.
FIXED_SLOTS = {
    "media_type": {"TV": 0, "Films": 1, "Music": 2},
    "decision": {"Direct Play": 2, "Direct Stream": 0, "Transcode": 1},
    "resolution": {"Played as 4K": 6, "Played as 1080p": 0, "Played as 720p": 1, "Played as 576p": 3,
                   "Played as 480p": 4, "Played as SD": 7, "Played as Unknown": 5},
}


def _slotter(conn: sqlite3.Connection, split: str | None):
    if split == "user":
        ranked = [r[0] for r in conn.execute(
            "SELECT u.id FROM users u LEFT JOIN plays p ON p.user_id = u.id GROUP BY u.id "
            "ORDER BY COUNT(p.id) DESC, u.id")]
        names = user_names(conn)
        slots = {names.get(uid, str(uid)): i for i, uid in enumerate(ranked)}
        return lambda name: slots.get(name, 7) % 8
    fixed = FIXED_SLOTS.get(split or "", {})
    return lambda name: fixed.get(name, 0)


@dataclass
class Range:
    start: int            # inclusive, epoch seconds
    end: int              # exclusive
    tz: ZoneInfo
    label: str

    @property
    def days(self) -> int:
        return max(1, round((self.end - self.start) / 86400))

    def auto_group(self) -> str:
        if self.days <= 92:
            return "day"
        if self.days <= 730:
            return "week"
        return "month"


def _midnight(d: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(d, dtime.min, tz).timestamp())


def make_range(conn: sqlite3.Connection, *, preset: str | None = None, date_from: str | None = None,
               date_to: str | None = None, now: float | None = None) -> Range:
    """'30d' etc, 'all', or explicit YYYY-MM-DD dates (inclusive), in local time."""
    tz = _tz(conn)
    today = datetime.fromtimestamp(now or time.time(), tz).date()
    if date_from or date_to:
        try:
            d0 = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else today - timedelta(days=29)
            d1 = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else today
        except ValueError:
            return make_range(conn, preset="30d", now=now)
        if d1 < d0:
            d0, d1 = d1, d0
        return Range(_midnight(d0, tz), _midnight(d1 + timedelta(days=1), tz), tz,
                     f"{d0:%d %b %Y} – {d1:%d %b %Y}")
    if preset == "all":
        first = conn.execute("SELECT MIN(started_at) FROM plays").fetchone()[0]
        d0 = datetime.fromtimestamp(first, tz).date() if first else today
        return Range(_midnight(d0, tz), _midnight(today + timedelta(days=1), tz), tz, "All time")
    if preset == "today":
        return Range(_midnight(today, tz), _midnight(today + timedelta(days=1), tz), tz, "Today")
    days = PRESETS.get(preset or "30d", 30)
    d0 = today - timedelta(days=days - 1)
    label = {7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days", 365: "Last 12 months"}[days]
    return Range(_midnight(d0, tz), _midnight(today + timedelta(days=1), tz), tz, label)


def range_from_args(conn: sqlite3.Connection, args) -> Range:
    return make_range(conn, preset=args.get("range") or None, date_from=args.get("from") or None,
                      date_to=args.get("to") or None)


def _tz(conn: sqlite3.Connection) -> ZoneInfo:
    try:
        return ZoneInfo(settings.get(conn, "ui.timezone"))
    except Exception:  # noqa: BLE001
        return ZoneInfo("Europe/London")


# -- row access --------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, rng: Range, *, user_id: int | None = None,
          library_id: int | None = None, extra: str = "") -> list[sqlite3.Row]:
    where = ["p.started_at >= ?", "p.started_at < ?"]
    params: list = [rng.start, rng.end]
    if user_id is not None:
        where.append("p.user_id = ?")
        params.append(user_id)
    if library_id is not None:
        where.append("p.library_id = ?")
        params.append(library_id)
    return conn.execute(
        f"""SELECT p.id, p.group_id, p.user_id, p.started_at, p.stopped_at, p.play_s, p.media_type,
                   p.decision, p.platform, p.product, p.player_title, p.src_resolution, p.dst_resolution,
                   p.library_id, p.grandparent_title, p.title, p.is_local {extra}
            FROM plays p WHERE {' AND '.join(where)} ORDER BY p.started_at""", params).fetchall()


def _value(row, metric: str) -> float:
    if metric == "watch_time":
        return row["play_s"] or 0
    return 1 if row["group_id"] is None else 0


def _local(ts: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


# -- summary -----------------------------------------------------------------------

def summary(conn: sqlite3.Connection, rng: Range, *, user_id: int | None = None,
            library_id: int | None = None) -> dict:
    rows = _rows(conn, rng, user_id=user_id, library_id=library_id)
    heads = [r for r in rows if r["group_id"] is None]
    peak, peak_at = concurrent_peak(conn, rng, user_id=user_id, library_id=library_id)
    transcodes = sum(1 for r in heads if r["decision"] == "transcode")
    return {
        "plays": len(heads),
        "watch_time_s": sum(r["play_s"] or 0 for r in rows),
        "users": len({r["user_id"] for r in rows}),
        "titles": len({(r["grandparent_title"] or r["title"]) for r in rows}),
        "transcode_pct": round(transcodes / len(heads) * 100) if heads else 0,
        "remote_pct": round(sum(1 for r in heads if r["is_local"] == 0) / len(heads) * 100) if heads else 0,
        "peak_concurrent": peak,
        "peak_at": peak_at,
        "range": {"start": rng.start, "end": rng.end, "label": rng.label},
    }


def concurrent_peak(conn: sqlite3.Connection, rng: Range, *, user_id: int | None = None,
                    library_id: int | None = None) -> tuple[int, int | None]:
    events = []
    for r in _rows(conn, rng, user_id=user_id, library_id=library_id):
        events.append((r["started_at"], 1))
        events.append((max(r["stopped_at"], r["started_at"]), -1))
    events.sort(key=lambda e: (e[0], e[1]))   # stops before starts at the same second
    cur = peak = 0
    peak_at = None
    for ts, d in events:
        cur += d
        if cur > peak:
            peak, peak_at = cur, ts
    return peak, peak_at


# -- time series -------------------------------------------------------------------

def _bucket_keys(rng: Range, group: str) -> list[date]:
    d = _local(rng.start, rng.tz).date()
    last = _local(rng.end - 1, rng.tz).date()
    keys = []
    if group == "day":
        while d <= last:
            keys.append(d)
            d += timedelta(days=1)
    elif group == "week":
        d -= timedelta(days=d.weekday())
        while d <= last:
            keys.append(d)
            d += timedelta(days=7)
    else:
        d = d.replace(day=1)
        while d <= last:
            keys.append(d)
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return keys


def _bucket_of(ts: int, tz: ZoneInfo, group: str) -> date:
    d = _local(ts, tz).date()
    if group == "week":
        return d - timedelta(days=d.weekday())
    if group == "month":
        return d.replace(day=1)
    return d


def _label(d: date, group: str) -> str:
    if group == "month":
        return d.strftime("%b %Y")
    return d.strftime("%d %b")


def _split_name(conn: sqlite3.Connection, split: str | None, row, names: dict) -> str:
    if split == "media_type":
        return MEDIA_LABELS.get(row["media_type"], (row["media_type"] or "Other").title())
    if split == "decision":
        return DECISION_LABELS.get(row["decision"], "Unknown")
    if split == "user":
        return names.get(row["user_id"], str(row["user_id"]))
    return "Plays"


def user_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT id, COALESCE(friendly_name, username, CAST(id AS TEXT)) FROM users")}


def timeseries(conn: sqlite3.Connection, rng: Range, *, metric: str = "plays", group: str | None = None,
               split: str | None = None, user_id: int | None = None, library_id: int | None = None) -> dict:
    metric = metric if metric in METRICS else "plays"
    group = group if group in GROUPS else rng.auto_group()
    split = split if split in SPLITS else None
    keys = _bucket_keys(rng, group)
    index = {k: i for i, k in enumerate(keys)}
    names = user_names(conn) if split == "user" else {}
    series: dict[str, list[float]] = defaultdict(lambda: [0] * len(keys))
    for r in _rows(conn, rng, user_id=user_id, library_id=library_id):
        i = index.get(_bucket_of(r["started_at"], rng.tz, group))
        if i is None:
            continue
        series[_split_name(conn, split, r, names)][i] += _value(r, metric)
    out = _limit_series(series)
    slot = _slotter(conn, split)
    return {"metric": metric, "group": group, "split": split,
            "labels": [_label(k, group) for k in keys],
            "series": [{"name": n, "data": _hours(d, metric), "slot": slot(n)} for n, d in out]}


def _limit_series(series: dict[str, list[float]]) -> list[tuple[str, list[float]]]:
    ranked = sorted(series.items(), key=lambda kv: -sum(kv[1]))
    if len(ranked) <= MAX_SERIES:
        return ranked
    keep, rest = ranked[:MAX_SERIES - 1], ranked[MAX_SERIES - 1:]
    other = [sum(vals) for vals in zip(*(v for _, v in rest))]
    return keep + [("Other", other)]


def _hours(data: list[float], metric: str) -> list[float]:
    """Watch time is sent as hours (1 dp), plays as whole numbers."""
    if metric == "watch_time":
        return [round(v / 3600, 1) for v in data]
    return [int(v) for v in data]


# -- distributions -----------------------------------------------------------------

def by_period(conn: sqlite3.Connection, rng: Range, *, dim: str = "weekday", metric: str = "plays",
              split: str | None = None, user_id: int | None = None, library_id: int | None = None) -> dict:
    """Totals by weekday (Mon..Sun), hour (0..23) or calendar month."""
    if dim == "hour":
        labels = [f"{h:02d}" for h in range(24)]
        key = lambda d: d.hour  # noqa: E731
    elif dim == "month":
        labels = [date(2000, m, 1).strftime("%b") for m in range(1, 13)]
        key = lambda d: d.month - 1  # noqa: E731
    else:
        dim = "weekday"
        labels = WEEKDAYS
        key = lambda d: d.weekday()  # noqa: E731
    names = user_names(conn) if split == "user" else {}
    series: dict[str, list[float]] = defaultdict(lambda: [0] * len(labels))
    for r in _rows(conn, rng, user_id=user_id, library_id=library_id):
        series[_split_name(conn, split, r, names)][key(_local(r["started_at"], rng.tz))] += _value(r, metric)
    slot = _slotter(conn, split)
    return {"metric": metric, "dim": dim, "labels": labels,
            "series": [{"name": n, "data": _hours(d, metric), "slot": slot(n)} for n, d in _limit_series(series)]}


def heatmap(conn: sqlite3.Connection, rng: Range, *, metric: str = "watch_time", user_id: int | None = None,
            library_id: int | None = None) -> dict:
    """Weekday × hour. Watch time is spread across the hours a play actually
    covered, so a 3-hour film at 20:00 lights up 20, 21 and 22."""
    grid = [[0.0] * 24 for _ in range(7)]
    for r in _rows(conn, rng, user_id=user_id, library_id=library_id):
        if metric == "plays":
            if r["group_id"] is None:
                d = _local(r["started_at"], rng.tz)
                grid[d.weekday()][d.hour] += 1
            continue
        remaining = r["play_s"] or 0
        t = r["started_at"]
        while remaining > 0:
            d = _local(t, rng.tz)
            in_hour = min(remaining, 3600 - (d.minute * 60 + d.second))
            grid[d.weekday()][d.hour] += in_hour
            remaining -= in_hour
            t += in_hour
    data = []
    for wd in range(7):
        for h in range(24):
            v = grid[wd][h]
            data.append([h, wd, round(v / 3600, 1) if metric == "watch_time" else int(v)])
    return {"metric": metric, "hours": [f"{h:02d}" for h in range(24)], "days": WEEKDAYS, "data": data,
            "max": max((p[2] for p in data), default=0)}


BREAKDOWNS = {
    "platform": "platform", "product": "product", "player": "player_title", "decision": "decision",
    "media_type": "media_type", "src_resolution": "src_resolution", "dst_resolution": "dst_resolution",
    "library": "library_id", "user": "user_id", "location": "is_local",
}


def breakdown(conn: sqlite3.Connection, rng: Range, *, field: str = "platform", metric: str = "plays",
              limit: int = 10, user_id: int | None = None, library_id: int | None = None) -> dict:
    col = BREAKDOWNS.get(field, "platform")
    c: Counter = Counter()
    names = {}
    if field == "user":
        names = user_names(conn)
    elif field == "library":
        names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM libraries")}
    for r in _rows(conn, rng, user_id=user_id, library_id=library_id):
        raw = r[col]
        if field == "decision":
            name = DECISION_LABELS.get(raw, "Unknown")
        elif field == "media_type":
            name = MEDIA_LABELS.get(raw, (raw or "Other").title())
        elif field in ("src_resolution", "dst_resolution"):
            name = _res_label(raw)
        elif field == "location":
            name = "LAN" if raw == 1 else "Remote" if raw == 0 else "Unknown"
        elif field in ("user", "library"):
            name = names.get(raw, "Unknown" if raw is None else str(raw))
        else:
            name = raw or "Unknown"
        c[name] += _value(r, metric)
    items = c.most_common()
    if field in ("src_resolution", "dst_resolution"):
        items.sort(key=lambda kv: -RESOLUTION_RANK.get(kv[0].lower().rstrip("p"), -1))
    if len(items) > limit:
        items = items[:limit - 1] + [("Other", sum(v for _, v in items[limit - 1:]))]
    return {"field": field, "metric": metric,
            "items": [{"name": n, "value": round(v / 3600, 1) if metric == "watch_time" else int(v)}
                      for n, v in items if v]}


def _res_label(v: str | None) -> str:
    if not v:
        return "Unknown"
    v = str(v).lower()
    if v in ("4k", "8k", "2k"):
        return v.upper()
    if v == "sd":
        return "SD"
    return f"{v}p" if v.isdigit() else v


def resolution_flow(conn: sqlite3.Connection, rng: Range) -> dict:
    """Source vs delivered resolution for video plays: how often quality is dropped."""
    c: Counter = Counter()
    for r in _rows(conn, rng):
        if r["media_type"] not in ("movie", "episode") or r["group_id"] is not None:
            continue
        c[(_res_label(r["src_resolution"]), _res_label(r["dst_resolution"] or r["src_resolution"]))] += 1
    labels = sorted({k for pair in c for k in pair}, key=lambda s: -RESOLUTION_RANK.get(s.lower().rstrip("p"), -1))
    slot = _slotter(conn, "resolution")
    return {"labels": labels, "series": [
        {"name": f"Played as {dst}", "data": [c.get((src, dst), 0) for src in labels], "slot": slot(f"Played as {dst}")}
        for dst in labels if any(c.get((src, dst), 0) for src in labels)]}


def concurrent_series(conn: sqlite3.Connection, rng: Range, group: str | None = None) -> dict:
    """Peak simultaneous streams per bucket."""
    group = group if group in GROUPS else rng.auto_group()
    keys = _bucket_keys(rng, group)
    index = {k: i for i, k in enumerate(keys)}
    peaks = [0] * len(keys)
    events = []
    for r in _rows(conn, rng):
        events.append((r["started_at"], 1))
        events.append((max(r["stopped_at"], r["started_at"]), -1))
    events.sort(key=lambda e: (e[0], e[1]))
    cur = 0
    for ts, d in events:
        cur += d
        if d > 0:
            i = index.get(_bucket_of(ts, rng.tz, group))
            if i is not None and cur > peaks[i]:
                peaks[i] = cur
    return {"group": group, "labels": [_label(k, group) for k in keys],
            "series": [{"name": "Peak streams", "data": peaks, "slot": 6}]}


# -- top lists -----------------------------------------------------------------------

TOP_KINDS = ("users", "shows", "movies", "artists", "platforms", "players")


def top(conn: sqlite3.Connection, rng: Range, *, kind: str = "shows", metric: str = "plays", limit: int = 10,
        user_id: int | None = None, library_id: int | None = None) -> dict:
    kind = kind if kind in TOP_KINDS else "shows"
    order = "plays DESC, play_s DESC" if metric == "plays" else "play_s DESC, plays DESC"
    where = ["p.started_at >= ?", "p.started_at < ?"]
    params: list = [rng.start, rng.end]
    if user_id is not None:
        where.append("p.user_id = ?")
        params.append(user_id)
    if library_id is not None:
        where.append("p.library_id = ?")
        params.append(library_id)
    w = " AND ".join(where)
    plays = "SUM(CASE WHEN p.group_id IS NULL THEN 1 ELSE 0 END) AS plays, SUM(p.play_s) AS play_s"
    if kind == "users":
        sql = (f"SELECT p.user_id AS id, COALESCE(u.friendly_name, u.username, CAST(p.user_id AS TEXT)) AS name, "
               f"NULL AS thumb, {plays}, MAX(p.started_at) AS last_played FROM plays p "
               f"LEFT JOIN users u ON u.id = p.user_id WHERE {w} GROUP BY p.user_id")
    elif kind == "shows":
        sql = (f"SELECT MAX(p.grandparent_rating_key) AS id, p.grandparent_title AS name, MAX(p.thumb) AS thumb, "
               f"{plays}, COUNT(DISTINCT p.user_id) AS users, MAX(p.started_at) AS last_played FROM plays p "
               f"WHERE {w} AND p.media_type = 'episode' AND p.grandparent_title IS NOT NULL "
               f"GROUP BY COALESCE(p.grandparent_guid, p.grandparent_title)")
    elif kind == "movies":
        sql = (f"SELECT MAX(p.rating_key) AS id, p.title AS name, MAX(p.thumb) AS thumb, MAX(p.year) AS year, "
               f"{plays}, COUNT(DISTINCT p.user_id) AS users, MAX(p.started_at) AS last_played FROM plays p "
               f"WHERE {w} AND p.media_type = 'movie' GROUP BY COALESCE(p.guid, p.title)")
    elif kind == "artists":
        sql = (f"SELECT MAX(p.grandparent_rating_key) AS id, p.grandparent_title AS name, MAX(p.thumb) AS thumb, "
               f"{plays}, COUNT(DISTINCT p.user_id) AS users, MAX(p.started_at) AS last_played FROM plays p "
               f"WHERE {w} AND p.media_type = 'track' GROUP BY p.grandparent_title")
    else:
        col = "p.product" if kind == "platforms" else "p.player_title"
        sql = (f"SELECT {col} AS id, COALESCE({col}, 'Unknown') AS name, NULL AS thumb, {plays}, "
               f"COUNT(DISTINCT p.user_id) AS users, MAX(p.started_at) AS last_played FROM plays p "
               f"WHERE {w} GROUP BY {col}")
    rows = conn.execute(f"{sql} ORDER BY {order} LIMIT ?", params + [max(1, min(limit, 100))]).fetchall()
    return {"kind": kind, "metric": metric, "items": [dict(r) for r in rows]}


def recently_added(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    """Newest films, shows with new episodes, and albums, one card per show."""
    rows = conn.execute(
        """SELECT CASE WHEN mi.type = 'episode' THEN mi.grandparent_rating_key ELSE mi.rating_key END AS rating_key,
                  CASE WHEN mi.type = 'episode' THEN mi.grandparent_title ELSE mi.title END AS title,
                  MAX(mi.added_at) AS added_at, mi.type, COUNT(*) AS n,
                  (SELECT s.thumb FROM media_items s WHERE s.rating_key =
                     CASE WHEN mi.type = 'episode' THEN mi.grandparent_rating_key ELSE mi.rating_key END) AS thumb,
                  MAX(mi.year) AS year, MAX(mi.parent_title) AS parent_title
           FROM media_items mi
           WHERE mi.is_present = 1 AND mi.type IN ('movie', 'episode', 'album') AND mi.added_at IS NOT NULL
           GROUP BY 1, mi.type ORDER BY added_at DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]
