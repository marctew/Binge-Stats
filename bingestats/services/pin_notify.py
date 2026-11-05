"""Messages about pinned shows (docs/specs/pins.md, stage 3): arrived, air date moved, and the week ahead.

Each event is for one person, the one who pinned the show (with notify on), as `pinner`, so a rule
can send each person their own with the condition "Pinned by is one of ...". Every episode, move
and week is recorded per person in pin_notified before it's sent, so nothing is announced twice:
not when Sonarr upgrades a file and Plex re-adds the episode, and not when a sync runs again.

- Arrived comes from the recently-added digest (worker), so a season drop is one message.
- Moved is found by the Sonarr sync, which runs in the job thread; it queues the events in
  pin_events and the worker sends them (the notifier lives in the worker).
- The week ahead goes out on Monday at 08:00 in ui.timezone.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta

from . import pins, settings, stats

ARRIVED, MOVED, WEEK, DISCOVER = "pin_arrived", "pin_schedule_changed", "pins_week", "pins_discover"
MOVE_MIN_S = 3600          # a shift smaller than this is Sonarr tidying up, not news
WEEK_HOUR = 8


def _code(season, episode) -> str:
    if season is None or episode is None:
        return ""
    return f"S{int(season):02d}E{int(episode):02d}"


def _pinners(conn: sqlite3.Connection, tvdb_id: int) -> list[dict]:
    return [{"id": r[0], "name": r[1] or "Someone"} for r in conn.execute(
        "SELECT p.user_id, COALESCE(u.friendly_name, u.username) FROM pins p JOIN users u ON u.id = p.user_id "
        "WHERE p.tvdb_id = ? AND p.notify = 1 ORDER BY p.user_id", (tvdb_id,))]


def _claim(conn: sqlite3.Connection, user_id: int, keys: list[str], now: int) -> set[str]:
    """Record these as told to this person; return the ones that weren't already."""
    fresh = set()
    for k in keys:
        if conn.execute("INSERT OR IGNORE INTO pin_notified (user_id, key, at) VALUES (?, ?, ?)",
                        (user_id, k, now)).rowcount:
            fresh.add(k)
    return fresh


# -- arrived -------------------------------------------------------------------------------

def arrivals(conn: sqlite3.Connection, event: dict, now: float | None = None) -> list[dict]:
    """A recently-added TV event -> one event per person who pinned the show, with only the episodes
    they haven't been told about."""
    now = int(now or time.time())
    if event.get("kind") != "episode" or not event.get("show_rating_key"):
        return []
    show = conn.execute("SELECT tvdb_id, thumb FROM media_items WHERE rating_key = ?",
                        (event["show_rating_key"],)).fetchone()
    if show is None or not show["tvdb_id"]:
        return []
    tvdb = show["tvdb_id"]
    keyed = [(f"ep:{tvdb}:{i.get('season')}:{i.get('episode')}" if i.get("episode") is not None
              else f"ep:{tvdb}:{i.get('title')}", i) for i in event.get("items") or []]
    out = []
    for who in _pinners(conn, tvdb):
        fresh = _claim(conn, who["id"], [k for k, _ in keyed], now)
        items = [i for k, i in keyed if k in fresh]
        if not items:
            continue
        name = event.get("show") or "A show"
        if len(items) == 1:
            headline = " · ".join(x for x in (name, _code(items[0].get("season"), items[0].get("episode"))) if x)
        else:
            season = items[0].get("season")
            headline = f"{name} · Season {season} · {len(items)} episodes" if season is not None \
                else f"{name} · {len(items)} episodes"
        out.append({"pinner": who, "show": name, "season": event.get("season"), "count": len(items),
                    "items": items, "headline": headline, "poster": event.get("poster") or show["thumb"],
                    "key": f"{who['id']}:{event.get('key')}"})
    return out


# -- air date moved ------------------------------------------------------------------------

def air_dates(conn: sqlite3.Connection) -> dict[tuple, int | None]:
    """(series id, season, episode) -> air time, for pinned shows. Taken before and after a Sonarr sync."""
    return {(r[0], r[1], r[2]): r[3] for r in conn.execute(
        "SELECT e.series_id, e.season, e.episode, e.air_at FROM sonarr_episodes e "
        "JOIN sonarr_series s ON s.id = e.series_id WHERE s.tvdb_id IN (SELECT tvdb_id FROM pins)")}


def _when(conn, ts: int) -> str:
    return datetime.fromtimestamp(ts, stats._tz(conn)).strftime("%a %-d %b, %H:%M")


def queue_moves(conn: sqlite3.Connection, before: dict[tuple, int | None], now: float | None = None) -> int:
    """Compare air times with `before` and queue a message per pinner for each that moved by an hour or
    more and still matters (the old or new time is in the future). Returns how many were queued."""
    now = int(now or time.time())
    after = air_dates(conn)
    moved = [(k, before[k], after[k]) for k in after
             if before.get(k) and after[k] and abs(after[k] - before[k]) >= MOVE_MIN_S and max(before[k], after[k]) > now]
    if not moved:
        return 0
    series = {r["id"]: r for r in conn.execute("SELECT id, title, tvdb_id FROM sonarr_series")}
    titles = {(r[0], r[1], r[2]): r[3] for r in conn.execute("SELECT series_id, season, episode, title FROM sonarr_episodes")}
    queued = 0
    for (sid, season, episode), old, new in moved:
        s = series.get(sid)
        if s is None or (season == 0 and not settings.get(conn, "pins.show_specials")):
            continue
        poster = conn.execute("SELECT thumb FROM media_items WHERE type = 'show' AND tvdb_id = ? AND is_present = 1 "
                              "LIMIT 1", (s["tvdb_id"],)).fetchone()
        for who in _pinners(conn, s["tvdb_id"]):
            key = f"move:{s['tvdb_id']}:{season}:{episode}:{new}"
            if not _claim(conn, who["id"], [key], now):
                continue
            payload = {"pinner": who, "show": s["title"], "season": season, "episode": episode,
                       "code": _code(season, episode), "title": titles.get((sid, season, episode)),
                       "old_at": old, "new_at": new, "old_text": _when(conn, old), "new_text": _when(conn, new),
                       "later": new > old, "count": 1, "poster": poster["thumb"] if poster else None,
                       "key": f"{who['id']}:{key}"}
            conn.execute("INSERT INTO pin_events (trigger, payload, created_at) VALUES (?, ?, ?)",
                         (MOVED, json.dumps(payload), now))
            queued += 1
    return queued


def pending(conn: sqlite3.Connection, now: float | None = None) -> list[tuple[str, dict]]:
    """Queued events for the worker to send; marked sent as they're handed over."""
    now = int(now or time.time())
    rows = conn.execute("SELECT id, trigger, payload FROM pin_events WHERE sent_at IS NULL ORDER BY id").fetchall()
    if rows:
        conn.execute(f"UPDATE pin_events SET sent_at = ? WHERE id IN ({','.join('?' * len(rows))})",
                     [now, *[r["id"] for r in rows]])
    conn.execute("DELETE FROM pin_events WHERE sent_at IS NOT NULL AND sent_at < ?", (now - 30 * 86400,))
    return [(r["trigger"], json.loads(r["payload"])) for r in rows]


# -- the week ahead ------------------------------------------------------------------------

def _week_start(conn, now: float) -> datetime:
    """This week's Monday at WEEK_HOUR, local time."""
    tz = stats._tz(conn)
    local = datetime.fromtimestamp(now, tz)
    monday = (local - timedelta(days=local.weekday())).date()
    return datetime(monday.year, monday.month, monday.day, WEEK_HOUR, tzinfo=tz)


def week_due(conn: sqlite3.Connection, now: float | None = None) -> bool:
    now = now or time.time()
    start = _week_start(conn, now).timestamp()
    return now >= start and (settings.get(conn, "pins.last_week_at") or 0) < start


def week_events(conn: sqlite3.Connection, now: float | None = None) -> list[dict]:
    """One event per person with pins (notify on) and something airing in the next 7 days."""
    now = int(now or time.time())
    tz = stats._tz(conn)
    start = _week_start(conn, now)
    settings.set(conn, "pins.last_week_at", now)
    week = start.strftime("%G-W%V")
    out = []
    users = conn.execute("SELECT DISTINCT p.user_id, COALESCE(u.friendly_name, u.username) FROM pins p "
                         "JOIN users u ON u.id = p.user_id WHERE p.notify = 1 ORDER BY p.user_id").fetchall()
    for uid, name in users:
        eps = pins._episodes(conn, uid, now, now + 7 * 86400, now, tz)
        if not eps:
            continue
        if not _claim(conn, uid, [f"week:{week}"], now):
            continue
        by_day: dict = {}
        for r in pins.condense(eps):
            text = f"{r['show']} {_code(r['season'], r['ep_first'])}–E{r['ep_last']:02d} · {r['count']} episodes" \
                if r["count"] > 1 and r.get("contiguous") else \
                (f"{r['show']} · {r['count']} episodes" if r["count"] > 1 else
                 f"{r['show']} {_code(r['season'], r['episode'])}"
                 f"{' · ' + r['title'] if r.get('title') and r['title'] != 'TBA' else ''}")
            text += f" · {datetime.fromtimestamp(r['air_at'], tz).strftime('%H:%M')}"
            by_day.setdefault(r["day"], []).append({"show": r["show"], "text": text, "air_at": r["air_at"],
                                                    "count": r["count"]})
        days = [{"label": d.strftime("%A %-d %B"), "episodes": v} for d, v in sorted(by_day.items())]
        out.append({"pinner": {"id": uid, "name": name or "Someone"}, "days": days, "count": len(eps),
                    "show": None, "poster": None, "key": f"{uid}:week:{week}"})
    return out


def discover_events(conn: sqlite3.Connection, now: float | None = None) -> list[dict]:
    """Monday's nudge, alongside the week ahead: per person with pins, the shows on their Discover list
    they haven't been told about for this run (a new date, or a new season announced or filming)."""
    from . import outlook as outlook_svc
    from . import pin_lists
    now = int(now or time.time())
    tz = stats._tz(conn)
    out = []
    users = conn.execute("SELECT DISTINCT p.user_id, COALESCE(u.friendly_name, u.username) FROM pins p "
                         "JOIN users u ON u.id = p.user_id WHERE p.notify = 1 ORDER BY p.user_id").fetchall()
    for uid, name in users:
        rows = pin_lists.discover(conn, uid, now)
        keys = {f"disc:{r['tvdb_id']}:{r['outlook']}:"
                f"{datetime.fromtimestamp(r['next_at'], tz).date() if r['next_at'] else ''}": r for r in rows}
        fresh = _claim(conn, uid, list(keys), now)
        shows = []
        for k, r in keys.items():
            if k not in fresh:
                continue
            if r["outlook"] == outlook_svc.DATED and r["next_at"]:
                text = f"{r['title']} · back on {datetime.fromtimestamp(r['next_at'], tz).strftime('%a %-d %b')}"
            elif r["outlook"] == outlook_svc.IN_PRODUCTION:
                text = f"{r['title']} · filming a new season"
            else:
                text = f"{r['title']} · new season announced"
            shows.append({"show": r["title"], "text": text})
        if shows:
            out.append({"pinner": {"id": uid, "name": name or "Someone"}, "shows": shows, "count": len(shows),
                        "show": None, "poster": None, "key": f"{uid}:discover:{now}"})
    return out

