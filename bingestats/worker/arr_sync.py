"""Snapshot Sonarr, Radarr and SABnzbd into Binge Stats (worker only).

Each app is synced on its own: one failing doesn't stop the others, and its
error is kept in arr_status for Settings. Every sync replaces that app's
tables in one transaction, so pages never see half a sync.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from .. import db
from ..arr import parse
from ..arr.client import ArrClient, ArrError, SabClient
from ..services import arr as arr_svc

log = logging.getLogger("bingestats.arr")


def _insert(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    conn.executemany(f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [[r[c] for c in cols] for r in rows])


def _status(conn, app: str, ok: bool, now: int, *, error: str | None = None, version: str | None = None,
            info: dict | None = None) -> None:
    old = conn.execute("SELECT info FROM arr_status WHERE app = ?", (app,)).fetchone()
    merged = {**(json.loads(old["info"]) if old else {}), **(info or {})}
    conn.execute("INSERT INTO arr_status (app, ok, error, version, last_sync_at, info) VALUES (?, ?, ?, ?, ?, ?) "
                 "ON CONFLICT(app) DO UPDATE SET ok = excluded.ok, error = excluded.error, "
                 "version = COALESCE(excluded.version, arr_status.version), "
                 "last_sync_at = CASE WHEN excluded.ok = 1 THEN excluded.last_sync_at ELSE arr_status.last_sync_at END, "
                 "info = excluded.info", (app, int(ok), error, version, now if ok else None, json.dumps(merged)))


def _categories(client: ArrClient, field: str) -> list[str]:
    """The SABnzbd categories this app sends its downloads to (from its download client settings)."""
    try:
        clients = client.get("downloadclient")
    except ArrError:
        return []
    out = set()
    for c in clients or []:
        if c.get("implementation") == "Sabnzbd" and c.get("enable", True):
            for f in c.get("fields") or []:
                if f.get("name") == field and f.get("value"):
                    out.add(str(f["value"]))
    return sorted(out)


def sync_sonarr(conn: sqlite3.Connection, client: ArrClient, now: float) -> dict:
    version = (client.get("system/status") or {}).get("version")
    series = [parse.series(s) for s in client.get("series")]
    missing = [parse.episode(e) for e in client.paged("wanted/missing", monitored="true")]
    cutoff = [parse.episode_cutoff(e) for e in client.paged("wanted/cutoff", monitored="true", includeEpisodeFile="true")]
    day = datetime.fromtimestamp(now, timezone.utc)
    upcoming = [parse.episode(e) for e in client.get("calendar", start=day.strftime("%Y-%m-%d"),
                                                     end=(day + timedelta(days=8)).strftime("%Y-%m-%d"))]
    with db.transaction(conn):
        for t in ("sonarr_series", "sonarr_missing", "sonarr_upcoming"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM arr_cutoff WHERE app = 'sonarr'")
        _insert(conn, "sonarr_series", series)
        _insert(conn, "sonarr_missing", [{k: e[k] for k in ("series_id", "season", "episode", "title", "air_at")}
                                         for e in missing if e["series_id"] is not None])
        _insert(conn, "sonarr_upcoming", [e for e in upcoming if e["series_id"] is not None])
        _insert(conn, "arr_cutoff", cutoff)
        info = {"series": len(series), "missing": len(missing), "cutoff": len(cutoff), "upcoming": len(upcoming),
                "categories": _categories(client, "tvCategory")}
        _status(conn, "sonarr", True, int(now), version=version, info=info)
    return info


def sync_radarr(conn: sqlite3.Connection, client: ArrClient, now: float) -> dict:
    version = (client.get("system/status") or {}).get("version")
    movies = [parse.movie(m) for m in client.get("movie")]
    try:
        collections = [parse.collection(c) for c in client.get("collection")]
    except ArrError:                        # older Radarr without the endpoint
        collections = []
    with db.transaction(conn):
        conn.execute("DELETE FROM radarr_movies")
        conn.execute("DELETE FROM radarr_collections")
        conn.execute("DELETE FROM arr_cutoff WHERE app = 'radarr'")
        _insert(conn, "radarr_movies", movies)
        _insert(conn, "radarr_collections", collections)
        _insert(conn, "arr_cutoff", [{"app": "radarr", "item_id": m["id"], "series_id": None, "season": None,
                                      "episode": None, "title": m["title"], "year": m["year"], "quality": m["quality"]}
                                     for m in movies if m["cutoff_not_met"]])
        info = {"movies": len(movies), "collections": len(collections), "categories": _categories(client, "movieCategory"),
                "missing": sum(1 for m in movies if m["monitored"] and m["is_available"] and not m["has_file"])}
        _status(conn, "radarr", True, int(now), version=version, info=info)
    return info


def sync_sab(conn: sqlite3.Connection, client: SabClient, now: float) -> dict:
    version = (client.get("version") or {}).get("version")
    stats = client.get("server_stats")
    daily = parse.sab_daily(stats)
    history = [parse.sab_history(s) for s in (client.get("history", limit=500).get("history") or {}).get("slots") or []]
    queue = parse.sab_queue(client.get("queue").get("queue") or {})
    with db.transaction(conn):
        for day, b in daily.items():          # SAB keeps a year or so; keep ours beyond that
            conn.execute("INSERT INTO sab_daily (day, bytes) VALUES (?, ?) "
                         "ON CONFLICT(day) DO UPDATE SET bytes = MAX(sab_daily.bytes, excluded.bytes)", (day, b))
        _insert(conn, "sab_history", [h for h in history if h["nzo_id"]])
        info = {"total_bytes": int(stats.get("total") or 0), "days": len(daily), "history": len(history),
                "queue": queue, "queue_at": int(now)}
        _status(conn, "sabnzbd", True, int(now), version=version, info=info)
    return {k: v for k, v in info.items() if k != "queue"}


def run(conn: sqlite3.Connection, progress=None, now: float | None = None, clients: dict | None = None) -> dict:
    """Sync every configured app. Returns per-app results or errors; raises only if all fail."""
    now = now or time.time()
    clients = clients or arr_svc.clients(conn)
    results, errors = {}, {}
    steps = [("sonarr", sync_sonarr), ("radarr", sync_radarr), ("sabnzbd", sync_sab)]
    configured = [(a, f) for a, f in steps if a in clients]
    for i, (app, fn) in enumerate(configured):
        if progress:
            progress(int(i / max(1, len(configured)) * 100), f"Reading {app.title()}")
        try:
            results[app] = fn(conn, clients[app], now)
        except (ArrError, KeyError, TypeError, ValueError) as e:
            msg = str(e) if isinstance(e, ArrError) else f"Unexpected answer ({e.__class__.__name__})"
            log.warning("%s sync failed: %s", app, msg)
            with db.transaction(conn):
                _status(conn, app, False, int(now), error=msg)
            errors[app] = msg
    if configured and len(errors) == len(configured):
        raise ArrError("; ".join(f"{a}: {m}" for a, m in errors.items()))
    return {"synced": results, "errors": errors}
