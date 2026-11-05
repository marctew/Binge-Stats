"""The outside API's answers in the standard's shapes (docs/specs/api-standard.md).

Everything here reads the database only: it never calls Plex or the *arrs in a request, so a
display polling every few seconds costs a few queries, and status still answers during an import.
Times come in as epoch seconds and leave as ISO 8601 UTC (`...Z`), converted only here.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

from . import activity, alerts, pin_api, pin_lists, settings

STATUS_REFRESH_S = 15
APP_NAMES = {"sonarr": "Sonarr", "radarr": "Radarr", "sabnzbd": "SABnzbd", "tmdb": "TMDB"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _downloading(conn: sqlite3.Connection) -> int:
    """TV and film jobs in SABnzbd's queue, from the watcher's last read."""
    row = conn.execute("SELECT info FROM arr_status WHERE app = 'sabnzbd'").fetchone()
    info = json.loads(row["info"]) if row and row["info"] else {}
    return sum(1 for r in (info.get("live") or {}).get("rows") or [] if r.get("kind") == "job")


def _pins_ready(conn: sqlite3.Connection, now: float) -> int:
    uid = pin_api.resolve_user(conn, None)
    if uid is None:
        return 0
    return sum(r["waiting"] for r in pin_lists.ready(conn, uid, now))


def _stream_line(a) -> str:
    s = a.session
    what = s.grandparent_title or s.title or "Something"
    if s.media_type == "episode" and s.season_index is not None and s.episode_index is not None:
        what += f" S{int(s.season_index):02d}E{int(s.episode_index):02d}"
    elif s.media_type == "movie" and s.year:
        what += f" ({s.year})"
    return f"{a.user_name} · {what}, {round(a.progress_pct)}%"


def status(conn: sqlite3.Connection, url: str, now: float | None = None) -> dict:
    now = now or time.time()
    worker = activity.worker_status(conn, now)
    srv = conn.execute("SELECT is_up FROM servers ORDER BY id LIMIT 1").fetchone()
    active = activity.list_active(conn)
    totals = activity.totals(active)
    n_alerts = alerts.unacked_count(conn)
    failing = [APP_NAMES.get(r[0], r[0]) for r in conn.execute("SELECT app FROM arr_status WHERE ok = 0 ORDER BY app")]
    busy = [r[0] for r in conn.execute("SELECT DISTINCT kind FROM jobs WHERE status = 'running'")]

    streams, transcodes = totals["streams"], totals.get("transcode", 0)
    if streams:
        playing = _plural(streams, "stream") + (f", {transcodes} transcoding" if transcodes else "")
    else:
        playing = "Nothing playing"
    detail = _stream_line(active[0]) if active else None

    if not worker["plex_configured"]:
        state, headline = "error", "Plex isn't set up yet"
    elif srv is not None and not srv["is_up"]:
        state, headline = "error", "Plex is down"
    elif not worker["alive"]:
        state, headline = "error", "The worker isn't running"
    elif n_alerts or failing:
        state, headline = "warning", playing
        reasons = []
        if n_alerts:
            reasons.append(_plural(n_alerts, "alert") + " to look at")
        if failing:
            reasons.append(f"{_and(failing)} didn't answer last time")
        detail = "; ".join(reasons)
    elif busy:
        state, headline = "busy", playing
        detail = detail or ("Running: " + ", ".join(k.replace("_", " ") for k in busy))
    else:
        state, headline = "ok", playing

    counts = [
        {"key": "streams", "label": "Streams", "value": streams},
        {"key": "transcodes", "label": "Transcoding", "value": transcodes},
        {"key": "bandwidth_mbps", "label": "Bandwidth (Mbps)", "value": round(totals["bandwidth_kbps"] / 1000, 1)},
        {"key": "alerts", "label": "Alerts", "value": n_alerts, **({"tone": "warning"} if n_alerts else {})},
        {"key": "downloads", "label": "Downloading", "value": _downloading(conn)},
        {"key": "pins_ready", "label": "Ready to watch", "value": _pins_ready(conn, now)},
    ]
    out = {"app": "binge-stats", "state": state, "headline": headline}
    if detail:
        out["detail"] = detail
    out.update(counts=counts, url=url, generated_at=_iso(now), refresh_s=STATUS_REFRESH_S)
    return out


def public_url(conn: sqlite3.Connection, fallback: str) -> str:
    return (settings.get(conn, "ui.public_url") or fallback).rstrip("/") + "/"
