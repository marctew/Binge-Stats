"""Sonarr, Radarr and SABnzbd: settings, status and the queries pages use.

The worker fills the snapshot tables (worker/arr_sync.py); everything here
only reads them, plus saving the connection settings from the web process.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import settings, stats

APPS = {"sonarr": "Sonarr", "radarr": "Radarr", "sabnzbd": "SABnzbd"}


# -- settings ---------------------------------------------------------------------------

def configured(conn: sqlite3.Connection) -> dict[str, bool]:
    return {a: bool(settings.get(conn, f"{a}.url") and settings.get(conn, f"{a}.api_key")) for a in APPS}


def clients(conn: sqlite3.Connection) -> dict:
    from ..arr.client import ArrClient, SabClient
    out = {}
    for app, ok in configured(conn).items():
        if ok:
            cls = SabClient if app == "sabnzbd" else ArrClient
            out[app] = cls(settings.get(conn, f"{app}.url"), settings.get(conn, f"{app}.api_key"))
    return out


def view(conn: sqlite3.Connection) -> list[dict]:
    rows = {r["app"]: dict(r) for r in conn.execute("SELECT * FROM arr_status")}
    out = []
    for app, name in APPS.items():
        st = rows.get(app) or {}
        out.append({"app": app, "name": name, "url": settings.get(conn, f"{app}.url"),
                    "has_key": bool(settings.get(conn, f"{app}.api_key")), "ok": bool(st.get("ok")),
                    "error": st.get("error"), "version": st.get("version"), "last_sync_at": st.get("last_sync_at"),
                    "info": json.loads(st.get("info") or "{}")})
    return out


def save(conn: sqlite3.Connection, form) -> list[str]:
    """Save URL + key per app. A blank key keeps the saved one; 'clear_<app>' removes the connection.
    Returns the apps that changed (so the caller can queue a sync)."""
    changed = []
    for app in APPS:
        if form.get(f"clear_{app}") == "on":
            settings.set(conn, f"{app}.url", "")
            settings.set(conn, f"{app}.api_key", "")
            conn.execute("DELETE FROM arr_status WHERE app = ?", (app,))
            changed.append(app)
            continue
        url = (form.get(f"{app}.url") or "").strip().rstrip("/")
        key = (form.get(f"{app}.api_key") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            raise ValueError(f"{APPS[app]}: the URL should start with http:// or https://")
        if url != settings.get(conn, f"{app}.url") or key:
            settings.set(conn, f"{app}.url", url)
            if key:
                settings.set(conn, f"{app}.api_key", key)
            changed.append(app)
    return changed


def ready(conn: sqlite3.Connection, app: str) -> bool:
    """Configured and synced at least once."""
    row = conn.execute("SELECT last_sync_at FROM arr_status WHERE app = ?", (app,)).fetchone()
    return bool(configured(conn).get(app) and row and row["last_sync_at"])


# -- Sonarr -----------------------------------------------------------------------------

def _plex_shows(conn) -> dict[int, sqlite3.Row]:
    return {r["tvdb_id"]: r for r in conn.execute(
        "SELECT rating_key, tvdb_id, thumb FROM media_items WHERE type = 'show' AND is_present = 1 AND tvdb_id IS NOT NULL")}


def _ranges(nums: list[int]) -> str:
    from .collection import _ranges as r
    return r(sorted(nums))


def series_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT ended, monitored, episode_count, episode_file_count FROM sonarr_series").fetchall()
    complete = [r for r in rows if r["episode_count"] and r["episode_file_count"] >= r["episode_count"]]
    return {"series": len(rows), "complete_ended": sum(1 for r in complete if r["ended"]),
            "complete_continuing": sum(1 for r in complete if not r["ended"]),
            "behind": sum(1 for r in rows if r["episode_count"] and r["episode_file_count"] < r["episode_count"]),
            "missing_episodes": conn.execute("SELECT COUNT(*) FROM sonarr_missing").fetchone()[0]}


def sonarr_gaps(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Series with monitored, aired episodes missing, nearly-complete first."""
    plex = _plex_shows(conn)
    missing: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    for r in conn.execute("SELECT series_id, season, episode FROM sonarr_missing"):
        missing[r[0]][r[1]].append(r[2])
    out = []
    for s in conn.execute(f"SELECT * FROM sonarr_series WHERE id IN ({','.join('?' * len(missing))})",
                          list(missing)) if missing else []:
        seasons = {x["season"]: x for x in json.loads(s["seasons"])}
        holes = []
        for season in sorted(missing[s["id"]]):
            eps = sorted(missing[s["id"]][season])
            st = seasons.get(season) or {}
            holes.append({"season": season, "missing": eps, "text": _ranges(eps),
                          "have": st.get("files", 0), "of": st.get("episodes", 0),
                          "whole": st.get("files", 0) == 0})
        n = sum(len(h["missing"]) for h in holes)
        have = s["episode_file_count"] or 0
        p = plex.get(s["tvdb_id"])
        out.append({"series_id": s["id"], "title": s["title"], "year": s["year"], "status": s["status"],
                    "ended": bool(s["ended"]), "holes": holes, "missing_episodes": n, "have_episodes": have,
                    "complete_pct": min(99, int(have / (have + n) * 100)) if have + n else 0,
                    "rating_key": p["rating_key"] if p else None, "thumb": p["thumb"] if p else None,
                    "size_bytes": s["size_bytes"]})
    out.sort(key=lambda x: (-x["complete_pct"], -x["have_episodes"], x["title"] or ""))
    return out[:limit] if limit else out


def upcoming(conn: sqlite3.Connection, now: float | None = None, days: int = 7) -> list[dict]:
    """Monitored episodes airing in the next `days`, grouped by local day."""
    now = now or time.time()
    tz = stats._tz(conn)
    plex = _plex_shows(conn)
    rows = conn.execute(
        "SELECT u.*, s.title AS show, s.tvdb_id FROM sonarr_upcoming u JOIN sonarr_series s ON s.id = u.series_id "
        "WHERE u.monitored = 1 AND u.air_at >= ? AND u.air_at < ? ORDER BY u.air_at, s.title",
        (int(now) - 6 * 3600, int(now) + days * 86400)).fetchall()
    by_day: dict[date, list] = defaultdict(list)
    for r in rows:
        p = plex.get(r["tvdb_id"])
        by_day[datetime.fromtimestamp(r["air_at"], tz).date()].append(
            {"show": r["show"], "season": r["season"], "episode": r["episode"], "title": r["title"],
             "air_at": r["air_at"], "has_file": bool(r["has_file"]),
             "rating_key": p["rating_key"] if p else None, "thumb": p["thumb"] if p else None})
    today = datetime.fromtimestamp(now, tz).date()
    label = lambda d: "Today" if d == today else "Tomorrow" if d == today + timedelta(days=1) else d.strftime("%A %-d %B")  # noqa: E731
    return [{"day": d.isoformat(), "label": label(d), "episodes": eps} for d, eps in sorted(by_day.items())]


# -- cutoff (both) ------------------------------------------------------------------------

def below_cutoff(conn: sqlite3.Connection) -> list[dict]:
    """Below the quality you set in Sonarr/Radarr: films one by one, shows by season."""
    plex_shows = _plex_shows(conn)
    plex_movies = {r["tmdb_id"]: r for r in conn.execute(
        "SELECT rating_key, tmdb_id, thumb FROM media_items WHERE type = 'movie' AND is_present = 1 AND tmdb_id IS NOT NULL")}
    out = []
    seasons: dict[tuple, dict] = {}
    for r in conn.execute("SELECT c.*, s.title AS show, s.year AS show_year, s.tvdb_id FROM arr_cutoff c "
                          "LEFT JOIN sonarr_series s ON s.id = c.series_id WHERE c.app = 'sonarr'"):
        key = (r["series_id"], r["season"])
        p = plex_shows.get(r["tvdb_id"])
        e = seasons.setdefault(key, {"app": "sonarr", "title": r["show"] or "Unknown show", "year": r["show_year"],
                                     "season": r["season"], "episodes": [], "qualities": set(),
                                     "rating_key": p["rating_key"] if p else None, "thumb": p["thumb"] if p else None})
        e["episodes"].append(r["episode"])
        if r["quality"]:
            e["qualities"].add(r["quality"])
    for e in seasons.values():
        q = e.pop("qualities")
        out.append({**e, "text": _ranges(e["episodes"]), "count": len(e["episodes"]), "quality": ", ".join(sorted(q))})
    for r in conn.execute("SELECT c.*, m.tmdb_id FROM arr_cutoff c LEFT JOIN radarr_movies m ON m.id = c.item_id "
                          "WHERE c.app = 'radarr'"):
        p = plex_movies.get(r["tmdb_id"])
        out.append({"app": "radarr", "title": r["title"], "year": r["year"], "season": None, "episodes": [],
                    "text": "", "count": 1, "quality": r["quality"] or "",
                    "rating_key": p["rating_key"] if p else None, "thumb": p["thumb"] if p else None})
    out.sort(key=lambda x: (x["app"] != "radarr", x["title"] or "", x["season"] or 0))
    return out


# -- Radarr --------------------------------------------------------------------------------

def collections(conn: sqlite3.Connection, min_films: int = 2) -> list[dict]:
    """Film collections: how many you have, nearly-complete first."""
    have = {r[0] for r in conn.execute("SELECT tmdb_id FROM radarr_movies WHERE has_file = 1")}
    plex = {r["tmdb_id"]: r for r in conn.execute(
        "SELECT rating_key, tmdb_id, thumb FROM media_items WHERE type = 'movie' AND is_present = 1 AND tmdb_id IS NOT NULL")}
    out = []
    for c in conn.execute("SELECT * FROM radarr_collections"):
        films = sorted(json.loads(c["movies"]), key=lambda f: (f.get("year") or 9999, f.get("title") or ""))
        if len(films) < min_films:
            continue
        got = [f for f in films if f["tmdb_id"] in have]
        cover = next((plex[f["tmdb_id"]] for f in got if f["tmdb_id"] in plex), None)
        out.append({"tmdb_id": c["tmdb_id"], "title": c["title"], "total": len(films), "have": len(got),
                    "pct": int(len(got) / len(films) * 100),
                    "missing": [f for f in films if f["tmdb_id"] not in have],
                    "thumb": cover["thumb"] if cover else None, "rating_key": cover["rating_key"] if cover else None})
    out.sort(key=lambda x: (x["have"] == x["total"], -x["pct"], -x["have"], x["title"] or ""))
    return out


def missing_films(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT title, year, tmdb_id, imdb_id, status, added_at FROM radarr_movies "
        "WHERE monitored = 1 AND is_available = 1 AND has_file = 0 ORDER BY year DESC, title")]


def radarr_summary(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT COUNT(*) AS films, SUM(has_file) AS have, "
                     "SUM(CASE WHEN monitored = 1 AND is_available = 1 AND has_file = 0 THEN 1 ELSE 0 END) AS missing "
                     "FROM radarr_movies").fetchone()
    cols = collections(conn)
    return {"films": r["films"] or 0, "have": r["have"] or 0, "missing": r["missing"] or 0,
            "collections": len(cols), "complete_collections": sum(1 for c in cols if c["have"] == c["total"])}


# -- SABnzbd -------------------------------------------------------------------------------

def downloads(conn: sqlite3.Connection, now: float | None = None) -> dict:
    now = now or time.time()
    tz = stats._tz(conn)
    today = datetime.fromtimestamp(now, tz).date()
    daily = {r[0]: r[1] for r in conn.execute("SELECT day, bytes FROM sab_daily")}

    def since(days: int) -> int:
        start = (today - timedelta(days=days - 1)).isoformat()
        return sum(b for d, b in daily.items() if d >= start)
    st = conn.execute("SELECT info, last_sync_at FROM arr_status WHERE app = 'sabnzbd'").fetchone()
    info = json.loads(st["info"]) if st else {}
    months: dict[str, int] = defaultdict(int)
    for d, b in daily.items():
        months[d[:7]] += b
    first = min(daily) if daily else None
    span_days = (today - date.fromisoformat(first)).days + 1 if first else 0
    return {"today": daily.get(today.isoformat(), 0), "week": since(7), "month": since(30),
            "all": max(info.get("total_bytes") or 0, sum(daily.values())), "since": first,
            "per_day_avg": int(sum(daily.values()) / span_days) if span_days else 0,
            "busiest": max(daily.items(), key=lambda kv: kv[1]) if daily else None,
            "months": sorted(months.items()), "queue": info.get("queue"), "queue_at": info.get("queue_at"),
            "failed": [dict(r) for r in conn.execute(
                "SELECT * FROM sab_history WHERE status = 'Failed' ORDER BY completed_at DESC LIMIT 20")]}


def downloads_series(conn: sqlite3.Connection, days: int = 90, now: float | None = None) -> dict:
    """Chart data: GB per day for the last `days`, or per month for everything (days=0)."""
    now = now or time.time()
    today = datetime.fromtimestamp(now, stats._tz(conn)).date()
    daily = {r[0]: r[1] for r in conn.execute("SELECT day, bytes FROM sab_daily")}
    if days:
        keys = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
        labels = [k.strftime("%d %b") for k in keys]
        data = [round(daily.get(k.isoformat(), 0) / 1e9, 1) for k in keys]
    else:
        months: dict[str, int] = defaultdict(int)
        for d, b in daily.items():
            months[d[:7]] += b
        ks = sorted(months)
        labels = [datetime.strptime(k, "%Y-%m").strftime("%b %y") for k in ks]
        data = [round(months[k] / 1e9, 1) for k in ks]
    return {"labels": labels, "series": [{"name": "Downloaded", "data": data, "slot": 0}]}


def to_csv(kind: str, rows: list[dict]) -> str:
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    if kind == "gaps":
        w.writerow(["show", "year", "status", "complete_pct", "missing_episodes", "detail"])
        for r in rows:
            w.writerow([r["title"], r["year"] or "", r["status"] or "", r["complete_pct"], r["missing_episodes"],
                        "; ".join(f"S{h['season']:02d}: {'whole season' if h['whole'] else h['text']}" for h in r["holes"])])
    elif kind == "cutoff":
        w.writerow(["app", "title", "year", "season", "episodes", "current_quality"])
        for r in rows:
            w.writerow([r["app"], r["title"], r["year"] or "", r["season"] if r["season"] is not None else "",
                        r["text"], r["quality"]])
    elif kind == "collections":
        w.writerow(["collection", "have", "total", "missing"])
        for r in rows:
            w.writerow([r["title"], r["have"], r["total"],
                        "; ".join(f"{f['title']} ({f.get('year') or '?'})" for f in r["missing"])])
    else:
        w.writerow(["title", "year", "status", "tmdb_id", "imdb_id"])
        for r in rows:
            w.writerow([r["title"], r["year"] or "", r["status"] or "", r["tmdb_id"] or "", r["imdb_id"] or ""])
    return out.getvalue()
