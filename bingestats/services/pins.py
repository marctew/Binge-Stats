"""Pins: the shows each person follows, and the calendar of just those (docs/specs/pins.md).

A pin belongs to a Plex user (users.id) and names a show by TVDB id, the id
Sonarr and Plex agree on. The admin pins as the server owner; viewers pin as
themselves from /me. Pins live only here: nothing is written to Sonarr or Plex.

Episode state is worked out when a page asks, from Sonarr's snapshot
(sonarr_episodes), the library (media_items) and the live download queue, so it
is never stale in the database and needs no migration to change.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import settings, stats

AGENDA_DAYS = 14          # the calendar's "coming up"
OVERDUE_DAYS = 30         # how far back "aired, not arrived" looks; beyond this it's the back catalogue
GRACE_S = 48 * 3600       # aired this recently and not here yet: expected, not missing

AVAILABLE, ON_DISK, UNMONITORED, DOWNLOADING = "available", "on_disk", "unmonitored", "downloading"
AIRING_TODAY, UPCOMING, AWAITING, MISSING = "airing_today", "upcoming", "awaiting", "missing"

# What each state is called. Its badge colour is the template's business (macros/media.html).
LABELS = {
    AVAILABLE: "In Plex", ON_DISK: "Downloaded", DOWNLOADING: "Downloading", AIRING_TODAY: "Today",
    UPCOMING: "Upcoming", AWAITING: "Expected", MISSING: "Missing", UNMONITORED: "Not wanted",
}


def episode_state(air_at: int | None, *, now: float, tz, in_plex: bool, has_file: bool = False,
                  monitored: bool = True, downloading: bool = False) -> str:
    """One episode's state. First match wins:

    - in Plex outranks everything: "can I watch it" is the question;
    - Sonarr has the file but Plex hasn't got it yet (not scanned, wrong path): say so, not "missing";
    - unmonitored: Sonarr will never fetch it by design, so it's never missing;
    - in the download queue;
    - no air date yet: upcoming;
    - airs (or aired) today, in local time: today, whether it's 09:00 or 21:00;
    - then upcoming, expected (aired < 48 h ago) or missing.
    """
    if in_plex:
        return AVAILABLE
    if has_file:
        return ON_DISK
    if not monitored:
        return UNMONITORED
    if downloading:
        return DOWNLOADING
    if air_at is None:
        return UPCOMING
    if datetime.fromtimestamp(air_at, tz).date() == datetime.fromtimestamp(now, tz).date():
        return AIRING_TODAY
    if air_at > now:
        return UPCOMING
    return AWAITING if now - air_at <= GRACE_S else MISSING


# -- who and what --------------------------------------------------------------------------

def owner_id(conn: sqlite3.Connection) -> int | None:
    """The admin pins as the Plex server owner."""
    row = conn.execute("SELECT id FROM users WHERE is_owner = 1 ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def pinned(conn: sqlite3.Connection, user_id: int) -> set[int]:
    return {r[0] for r in conn.execute("SELECT tvdb_id FROM pins WHERE user_id = ?", (user_id,))}


def _shows(conn: sqlite3.Connection, tvdb_ids: list[int] | None = None) -> dict[int, dict]:
    """Shows known to Plex or Sonarr, by TVDB id: title, poster and where to link."""
    where, params = "", []
    if tvdb_ids is not None:
        if not tvdb_ids:
            return {}
        where, params = f" AND tvdb_id IN ({','.join('?' * len(tvdb_ids))})", list(tvdb_ids)
    out: dict[int, dict] = {}
    for r in conn.execute(f"SELECT id, title, year, tvdb_id, status, next_airing, previous_airing FROM sonarr_series "
                          f"WHERE tvdb_id IS NOT NULL{where}", params):
        out[r["tvdb_id"]] = {"tvdb_id": r["tvdb_id"], "title": r["title"], "year": r["year"], "series_id": r["id"],
                             "status": r["status"], "next_airing": r["next_airing"],
                             "previous_airing": r["previous_airing"], "rating_key": None, "thumb": None}
    for r in conn.execute(f"SELECT rating_key, title, year, tvdb_id, thumb FROM media_items WHERE type = 'show' "
                          f"AND is_present = 1 AND tvdb_id IS NOT NULL{where} ORDER BY rating_key", params):
        s = out.setdefault(r["tvdb_id"], {"tvdb_id": r["tvdb_id"], "series_id": None, "status": None,
                                          "next_airing": None, "previous_airing": None, "year": r["year"]})
        if s.get("rating_key") is None:
            s.update(title=r["title"], rating_key=r["rating_key"], thumb=r["thumb"], year=s.get("year") or r["year"])
    return out


def show(conn: sqlite3.Connection, tvdb_id: int) -> dict | None:
    return _shows(conn, [tvdb_id]).get(tvdb_id)


def pin(conn: sqlite3.Connection, user_id: int, tvdb_id: int, now: float | None = None) -> bool:
    """Pin a show. Returns True if it wasn't pinned before. Only shows in Plex or Sonarr can be pinned."""
    if show(conn, tvdb_id) is None:
        raise ValueError("That show isn't in Plex or Sonarr")
    cur = conn.execute("INSERT OR IGNORE INTO pins (user_id, tvdb_id, pinned_at) VALUES (?, ?, ?)",
                       (user_id, tvdb_id, int(now or time.time())))
    return cur.rowcount > 0


def unpin(conn: sqlite3.Connection, user_id: int, tvdb_id: int) -> bool:
    return conn.execute("DELETE FROM pins WHERE user_id = ? AND tvdb_id = ?", (user_id, tvdb_id)).rowcount > 0


def toggle(conn: sqlite3.Connection, user_id: int, tvdb_id: int, now: float | None = None) -> bool:
    """Pin or unpin; returns whether it's pinned now. Queues a read of the show's episode list from
    Sonarr on a new pin, so the calendar has its back catalogue without waiting for the next sync."""
    if unpin(conn, user_id, tvdb_id):
        return False
    pin(conn, user_id, tvdb_id, now)
    from . import arr, jobs
    if arr.configured(conn)["sonarr"]:
        jobs.ensure(conn, "pin_guides")
    return True


def find(conn: sqlite3.Connection, q: str, user_id: int, limit: int = 12) -> list[dict]:
    """Shows whose title contains q, pinned ones flagged. For the "pin a show" search."""
    q = (q or "").strip().lower()
    if len(q) < 2:
        return []
    mine = pinned(conn, user_id)
    hits = [s for s in _shows(conn).values() if q in (s.get("title") or "").lower()]
    hits.sort(key=lambda s: (not (s["title"] or "").lower().startswith(q), (s["title"] or "").lower()))
    return [{**s, "pinned": s["tvdb_id"] in mine} for s in hits[:limit]]


# -- the calendar --------------------------------------------------------------------------

def _in_plex(conn: sqlite3.Connection, tvdb_ids: list[int]) -> dict[tuple, int]:
    """(tvdb, season, episode) -> episode rating key, for episodes of these shows in the library."""
    if not tvdb_ids:
        return {}
    rows = conn.execute(
        "SELECT s.tvdb_id, e.season_index, e.episode_index, e.rating_key FROM media_items e "
        "JOIN media_items s ON s.rating_key = e.grandparent_rating_key "
        f"WHERE e.type = 'episode' AND e.is_present = 1 AND s.tvdb_id IN ({','.join('?' * len(tvdb_ids))})",
        tvdb_ids)
    return {(r[0], r[1], r[2]): r[3] for r in rows}


def downloading(conn: sqlite3.Connection) -> dict[tuple, int]:
    """(tvdb, season, episode) -> percent done, from the live download queue (if SABnzbd is set up)."""
    row = conn.execute("SELECT info FROM arr_status WHERE app = 'sabnzbd'").fetchone()
    info = json.loads(row["info"]) if row else {}
    live, known = info.get("live") or {}, info.get("live_map") or {}
    out: dict[tuple, int] = {}
    jobs = [(r.get("nzo_id"), r.get("pct") or 0) for r in live.get("rows") or [] if r.get("kind") == "job"]
    jobs += [(p.get("nzo_id"), 100) for p in live.get("pp") or []]
    for nzo, pct in jobs:
        for rec in known.get(nzo) or []:
            if rec.get("app") == "sonarr" and rec.get("tvdb_id") and rec.get("season") is not None:
                out[(rec["tvdb_id"], rec["season"], rec["episode"])] = int(pct)
    return out


def _day_label(d, today) -> str:
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return d.strftime("%A %-d %B")


def _episodes(conn: sqlite3.Connection, user_id: int, start: int, end: int, now: float, tz) -> list[dict]:
    """Pinned episodes airing in [start, end), with their state, in air order. Specials and episodes
    Sonarr isn't fetching are left out unless the settings say to show them."""
    show_specials = bool(settings.get(conn, "pins.show_specials"))
    show_unmonitored = bool(settings.get(conn, "pins.show_unmonitored"))
    tvdb_ids = sorted(pinned(conn, user_id))
    shows = _shows(conn, tvdb_ids)
    by_series = {s["series_id"]: s for s in shows.values() if s.get("series_id")}
    if not by_series:
        return []
    plex = _in_plex(conn, tvdb_ids)
    dl = downloading(conn)
    rows = conn.execute(
        f"SELECT * FROM sonarr_episodes WHERE series_id IN ({','.join('?' * len(by_series))}) "
        "AND air_at >= ? AND air_at < ? ORDER BY air_at, season, episode", [*by_series, start, end]).fetchall()
    out = []
    for r in rows:
        s = by_series[r["series_id"]]
        if r["season"] == 0 and not show_specials:
            continue
        if not r["monitored"] and not show_unmonitored:
            continue
        key = (s["tvdb_id"], r["season"], r["episode"])
        state = episode_state(r["air_at"], now=now, tz=tz, in_plex=key in plex, has_file=bool(r["has_file"]),
                              monitored=bool(r["monitored"]), downloading=key in dl)
        out.append({"show": s["title"], "tvdb_id": s["tvdb_id"], "series_id": s["series_id"],
                    "show_rating_key": s["rating_key"], "thumb": s["thumb"], "season": r["season"],
                    "episode": r["episode"], "title": r["title"], "air_at": r["air_at"], "state": state,
                    "label": LABELS[state], "rating_key": plex.get(key), "pct": dl.get(key),
                    "late_s": int(now - r["air_at"]) if r["air_at"] and r["air_at"] < now else 0,
                    "day": datetime.fromtimestamp(r["air_at"], tz).date()})
    return out


def month(conn: sqlite3.Connection, user_id: int, ym: str | None = None, now: float | None = None) -> dict:
    """One month of pinned episodes as Monday-first weeks, for the calendar's month view.
    ym is 'YYYY-MM' (this month if missing or not a month)."""
    now = now or time.time()
    tz = stats._tz(conn)
    today = datetime.fromtimestamp(now, tz).date()
    try:
        y, m = (int(x) for x in (ym or "").split("-"))
        first = date(y, m, 1)
    except ValueError:
        first = today.replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    grid_start = first - timedelta(days=first.weekday())
    grid_end = nxt + timedelta(days=(7 - nxt.weekday()) % 7)
    at = lambda d: int(datetime.combine(d, datetime.min.time(), tz).timestamp())  # noqa: E731
    by_day: dict = defaultdict(list)
    for ep in _episodes(conn, user_id, at(grid_start), at(grid_end), now, tz):
        by_day[ep["day"]].append(ep)
    weeks, d = [], grid_start
    while d < grid_end:
        weeks.append([{"date": d + timedelta(days=i), "in_month": (d + timedelta(days=i)).month == first.month,
                       "today": d + timedelta(days=i) == today, "episodes": by_day.get(d + timedelta(days=i), [])}
                      for i in range(7)])
        d += timedelta(days=7)
    prev = (first - timedelta(days=1)).replace(day=1)
    return {"title": first.strftime("%B %Y"), "ym": first.strftime("%Y-%m"), "weeks": weeks,
            "prev": prev.strftime("%Y-%m"), "next": nxt.strftime("%Y-%m"),
            "is_this_month": first == today.replace(day=1),
            "days": [day for week in weeks for day in week if day["in_month"] and day["episodes"]],
            "count": sum(len(day["episodes"]) for week in weeks for day in week if day["in_month"])}


def calendar(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> dict:
    """Everything /calendar shows for one person:

    - agenda: the next AGENDA_DAYS days, by local day;
    - overdue: aired before today (within OVERDUE_DAYS) and not in Plex, newest first;
    - later: pinned shows with a date past the agenda, soonest first;
    - undated: pinned shows with no date to come that aren't listed above (ended, no date yet,
      not in Sonarr), so every pin can be found and unpinned somewhere on the page;
    - undated_groups: those, by season outlook (announced, filming, hiatus, probably over, ...);
    - pins: every pin.
    """
    now = now or time.time()
    tz = stats._tz(conn)
    today = datetime.fromtimestamp(now, tz).date()
    start = int(datetime.combine(today - timedelta(days=OVERDUE_DAYS), datetime.min.time(), tz).timestamp())
    end = int(datetime.combine(today + timedelta(days=AGENDA_DAYS), datetime.min.time(), tz).timestamp())
    tvdb_ids = sorted(pinned(conn, user_id))
    shows = _shows(conn, tvdb_ids)

    agenda: dict = defaultdict(list)
    overdue, in_agenda, in_overdue = [], set(), set()
    for ep in _episodes(conn, user_id, start, end, now, tz):
        if ep["day"] >= today:
            agenda[ep["day"]].append(ep)
            in_agenda.add(ep["tvdb_id"])
        elif ep["state"] not in (AVAILABLE, UNMONITORED):   # aired and not watchable: expected, missing,
            overdue.append(ep)                              # downloading, or on disk but not in Plex
            in_overdue.add(ep["tvdb_id"])
    overdue.sort(key=lambda e: -e["air_at"])

    from . import outlook as outlook_svc
    looks = outlook_svc.for_shows(conn, tvdb_ids, now)
    by_title = sorted(shows.values(), key=lambda s: (s["title"] or "").lower())
    for s in by_title:
        o = looks.get(s["tvdb_id"]) or {"outlook": outlook_svc.UNKNOWN, "label": "Unknown", "next_at": None}
        s.update(outlook=o["outlook"], outlook_label=o["label"], next_airing=o["next_at"] or s["next_airing"])
        s["dated"] = bool(s["next_airing"] and s["next_airing"] >= now)
    later = sorted((s for s in by_title if s["dated"] and s["tvdb_id"] not in in_agenda),
                   key=lambda s: s["next_airing"])
    undated = [s for s in by_title if not s["dated"] and s["tvdb_id"] not in in_agenda | in_overdue]
    groups = [{"outlook": k, "label": label, "shows": [s for s in undated if s["outlook"] == k]}
              for k, label in outlook_svc.LABELS.items() if k != outlook_svc.DATED]

    return {"agenda": [{"day": d.isoformat(), "label": _day_label(d, today), "episodes": eps}
                       for d, eps in sorted(agenda.items())],
            "overdue": overdue, "later": later, "undated": undated, "pins": by_title,
            "undated_groups": [g for g in groups if g["shows"]],
            "count": len(tvdb_ids), "agenda_days": AGENDA_DAYS, "overdue_days": OVERDUE_DAYS}


def for_item(conn: sqlite3.Connection, item: dict, user_id: int | None) -> dict | None:
    """The pin button for an item page: the show's TVDB id and whether it's pinned. None if the
    item isn't a show (or part of one) with a TVDB id, or there's nobody to pin for."""
    if user_id is None or item.get("type") not in ("show", "season", "episode"):
        return None
    tvdb = item.get("tvdb_id") if item.get("type") == "show" else None
    if tvdb is None:
        rk = item.get("rating_key") if item.get("type") == "show" else item.get("grandparent_rating_key")
        row = conn.execute("SELECT tvdb_id FROM media_items WHERE rating_key = ?", (rk,)).fetchone() if rk else None
        tvdb = row["tvdb_id"] if row else None
    if not tvdb:
        return None
    from . import outlook as outlook_svc
    o = outlook_svc.for_shows(conn, [tvdb]).get(tvdb) or {}
    return {"tvdb_id": tvdb, "pinned": tvdb in pinned(conn, user_id), "outlook": o.get("outlook"),
            "next_at": o.get("next_at")}
