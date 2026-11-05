"""Pins over the REST API (docs/API.md): flat, JSON-ready shapes for scripts, Home Assistant, a wall
display and the Scriptable widget (docs/widget/). Routes stay thin; everything is built here.

Times are given twice: `*_at` in UTC epoch seconds (Binge's own rule) and `*_local` as ISO 8601 in
ui.timezone, so a shell script doesn't need a date library. The widget gets its wording pre-built,
so phrasing can change here without anybody reinstalling the script on their phone.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime

from . import activity, pin_lists, pins, settings, stats
from . import outlook as outlook_svc

WIDGET_STALE_MINUTES = 45


def _local(conn, ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, stats._tz(conn)).isoformat() if ts else None


def _code(season, episode) -> str:
    return f"S{int(season):02d}E{int(episode):02d}" if season is not None and episode is not None else ""


def episode(conn, e: dict) -> dict:
    """One calendar episode (from pins._episodes), flat."""
    return {"show": e["show"], "tvdb_id": e["tvdb_id"], "season": e["season"], "episode": e["episode"],
            "code": _code(e["season"], e["episode"]), "title": e.get("title"),
            "air_at": e["air_at"], "air_local": _local(conn, e["air_at"]), "state": e["state"], "label": e["label"],
            "downloading_pct": e.get("pct"), "stalled": bool(e.get("stalled")), "rating_key": e.get("rating_key"),
            "show_rating_key": e.get("show_rating_key")}


def pin_list(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> list[dict]:
    now = now or time.time()
    ids = sorted(pins.pinned(conn, user_id))
    shows = pins._shows(conn, ids)
    looks = outlook_svc.for_shows(conn, ids, now)
    meta = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT tvdb_id, pinned_at, notify FROM pins WHERE user_id = ?",
                                                     (user_id,))}
    out = []
    for t in ids:
        s, o = shows.get(t) or {}, looks.get(t) or {}
        out.append({"tvdb_id": t, "title": s.get("title"), "year": s.get("year"), "rating_key": s.get("rating_key"),
                    "in_sonarr": bool(s.get("series_id")), "outlook": o.get("outlook", outlook_svc.UNKNOWN),
                    "outlook_label": o.get("label", "Unknown"), "next_at": o.get("next_at"),
                    "next_local": _local(conn, o.get("next_at")), "pinned_at": meta.get(t, (None, 1))[0],
                    "notify": bool(meta.get(t, (None, 1))[1])})
    return sorted(out, key=lambda r: (r["title"] or "").lower())


def schedule(conn: sqlite3.Connection, user_id: int, days: int = 7, back: int = 1, now: float | None = None) -> list[dict]:
    """Pinned episodes airing from `back` days ago to `days` ahead, in air order."""
    now = now or time.time()
    return [episode(conn, e) for e in pins._episodes(conn, user_id, int(now - back * 86400), int(now + days * 86400),
                                                       now, stats._tz(conn))]


def arrivals(conn: sqlite3.Connection, user_id: int, hours: int = 24, now: float | None = None) -> list[dict]:
    """Episodes of pinned shows that arrived in Plex in the last `hours`: the trigger an automation wants."""
    now = int(now or time.time())
    ids = sorted(pins.pinned(conn, user_id))
    if not ids:
        return []
    rows = conn.execute(
        "SELECT s.title AS show, s.tvdb_id, e.season_index, e.episode_index, e.title, e.added_at, e.rating_key "
        "FROM media_items e JOIN media_items s ON s.rating_key = e.grandparent_rating_key "
        f"WHERE e.type = 'episode' AND e.is_present = 1 AND s.tvdb_id IN ({','.join('?' * len(ids))}) AND e.added_at >= ? "
        "ORDER BY e.added_at DESC, s.title, e.season_index, e.episode_index", [*ids, now - hours * 3600]).fetchall()
    return [{"show": r["show"], "tvdb_id": r["tvdb_id"], "season": r["season_index"], "episode": r["episode_index"],
             "code": _code(r["season_index"], r["episode_index"]), "title": r["title"], "added_at": r["added_at"],
             "added_local": _local(conn, r["added_at"]), "rating_key": r["rating_key"]} for r in rows]


def _downloads(conn: sqlite3.Connection, user_id: int) -> tuple[int, int]:
    """(jobs in SABnzbd's queue for this person's pinned shows, how many of those are stalled)."""
    row = conn.execute("SELECT info FROM arr_status WHERE app = 'sabnzbd'").fetchone()
    info = json.loads(row["info"]) if row else {}
    live, known = info.get("live") or {}, info.get("live_map") or {}
    mine = pins.pinned(conn, user_id)
    jobs = stalled = 0
    for r in live.get("rows") or []:
        recs = known.get(r.get("nzo_id")) or []
        if r.get("kind") == "job" and any(x.get("app") == "sonarr" and x.get("tvdb_id") in mine for x in recs):
            jobs += 1
            stalled += bool(r.get("stalled_s"))
    return jobs, stalled


def _when(conn, ts: int, now: float) -> str:
    """'20:00', 'tomorrow 21:00', 'Fri 21:00', '14 Nov': a phone has no room for more."""
    tz = stats._tz(conn)
    air, today = datetime.fromtimestamp(ts, tz), datetime.fromtimestamp(now, tz).date()
    days = (air.date() - today).days
    clock = air.strftime("%H:%M")
    if days == 0:
        return clock
    if days == 1:
        return f"tomorrow {clock}"
    if 2 <= days <= 6:
        return f"{air.strftime('%a')} {clock}"
    return air.strftime("%-d %b")


def summary(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> dict:
    """Everything a dashboard needs in one call, all from the same instant: not RESTful on purpose,
    so a wall display redrawing every minute makes one request, not six."""
    now = now or time.time()
    cal = pins.calendar(conn, user_id, now)
    ready = pin_lists.ready(conn, user_id, now)
    nxt = pins.next_up(conn, user_id, now)
    jobs, stalled = _downloads(conn, user_id)
    tz = stats._tz(conn)
    today = datetime.fromtimestamp(now, tz).date()
    todays = [e for d in cal["agenda"] for e in d["episodes"] if d["day"] == today.isoformat()]
    failing = [r[0] for r in conn.execute("SELECT app FROM arr_status WHERE ok = 0")]
    return {
        "generated_at": int(now), "generated_local": _local(conn, int(now)), "user_id": user_id,
        "healthy": activity.worker_status(conn)["alive"] and not failing, "failing": failing,
        "pins": cal["count"],
        "next": episode(conn, nxt) if nxt else None,
        "today": [episode(conn, e) for e in todays],
        "overdue": {"total": len(cal["overdue"]),
                    "missing": sum(1 for e in cal["overdue"] if e["state"] == pins.MISSING)},
        "ready": {"shows": len(ready), "episodes": sum(r["waiting"] for r in ready),
                  "top": [{"show": r["show"], "next": r["next_code"], "waiting": r["waiting"], "new": r["new"]}
                          for r in ready[:5]]},
        "downloads": {"jobs": jobs, "stalled": stalled},
    }


def widget(conn: sqlite3.Connection, user_id: int, public_url: str, now: float | None = None) -> dict:
    """A home-screen widget's worth, worded here. Shape kept from Pinnarr's widget, so it reads the same."""
    now = now or time.time()
    nxt = pins.next_up(conn, user_id, now)
    ready = pin_lists.ready(conn, user_id, now)
    cal = pins.calendar(conn, user_id, now)
    jobs, stalled = _downloads(conn, user_id)
    tz = stats._tz(conn)
    today = datetime.fromtimestamp(now, tz).date()
    tonight = [e for d in cal["agenda"] if d["day"] == today.isoformat() for e in d["episodes"]
               if e["state"] in (pins.AIRING_TODAY, pins.UPCOMING) and e["air_at"] >= now]
    missing = sum(1 for e in cal["overdue"] if e["state"] == pins.MISSING)
    code = ""
    if nxt:
        # a season dropping at once is "S06E01–E06", not its first episode
        same = [e for e in pins._episodes(conn, user_id, nxt["air_at"], nxt["air_at"] + 1, now, tz)
                if e["tvdb_id"] == nxt["tvdb_id"] and e["season"] == nxt["season"] and e["state"] == nxt["state"]]
        last = max((e["episode"] for e in same), default=nxt["episode"])
        code = _code(nxt["season"], nxt["episode"]) + (f"–E{last:02d}" if last != nxt["episode"] else "")
    if nxt:
        headline = f"{nxt['show']} {code}"
        subhead = _when(conn, nxt["air_at"], now) + (f" · {len(tonight) - 1} more tonight" if len(tonight) > 1 else "")
    elif ready:
        headline, subhead = f"{len(ready)} ready to watch", f"{ready[0]['show']} next"
    else:
        headline, subhead = "Nothing coming", "for anything you've pinned"
    user = conn.execute("SELECT COALESCE(friendly_name, username) FROM users WHERE id = ?", (user_id,)).fetchone()
    return {
        "generated_at": int(now), "stale_after_minutes": WIDGET_STALE_MINUTES,
        "user": user[0] if user else None, "headline": headline, "subhead": subhead, "tonight": len(tonight),
        "next": ({"series": nxt["show"], "code": code, "title": nxt.get("title") if code.count("E") == 1 else None,
                  "when": _when(conn, nxt["air_at"], now), "state": nxt["state"], "label": nxt["label"],
                  "poster": f"/api/v1/pins/poster/{nxt['tvdb_id']}"} if nxt else None),
        # Drawn generically by the widget, so a stat added here shows up without reinstalling anything.
        "stats": [
            {"label": "Ready", "value": str(len(ready)), "detail": f"{sum(r['waiting'] for r in ready)} eps" if ready else "",
             "alert": False},
            {"label": "Downloading", "value": str(jobs), "detail": "", "alert": False},
            {"label": "Stalled", "value": str(stalled), "detail": "", "alert": stalled > 0},
            {"label": "Missing", "value": str(missing), "detail": "", "alert": missing > 0},
        ],
        "url": (public_url.rstrip("/") + "/calendar") if public_url else None,
    }


def poster_source(conn: sqlite3.Connection, tvdb_id: int) -> tuple[str, str | int] | None:
    """Where a show's poster comes from: ('plex', thumb path) or ('sonarr', series id)."""
    s = pins.show(conn, tvdb_id)
    if s is None:
        return None
    if s.get("thumb"):
        return "plex", s["thumb"]
    if s.get("series_id"):
        return "sonarr", s["series_id"]
    return None


def resolve_user(conn: sqlite3.Connection, user_id: int | None) -> int | None:
    """The person a request is about: ?user_id if it's a known user, else the Plex owner."""
    if user_id is not None:
        return user_id if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() else None
    return pins.owner_id(conn)


def public_url(conn: sqlite3.Connection) -> str:
    return settings.get(conn, "ui.public_url") or ""
