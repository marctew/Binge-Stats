"""Alerts: stream/transcode limits, sharing signs and server up/down."""
from __future__ import annotations

import json
import sqlite3

KINDS = {
    "stream_limit": "Stream limit",
    "transcode_limit": "Transcode limit",
    "concurrent_ips": "Two places at once",
    "new_country": "New country",
    "server_down": "Plex down",
    "server_up": "Plex back",
}


def _dur(s) -> str:
    s = int(s or 0)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m" if m else f"{s}s"


def describe(kind: str, name: str | None, d: dict) -> str:
    who = name or "Someone"
    if kind in ("stream_limit", "transcode_limit"):
        what = "streams" if kind == "stream_limit" else "transcodes"
        text = f"{who} had {d.get('count')} {what} at once (limit {d.get('limit')})"
        return text + (". The newest was stopped." if d.get("stopped") else ".")
    if kind == "concurrent_ips":
        places = " and ".join(p.get("place", "?") for p in d.get("places") or []) or ", ".join(d.get("ips") or [])
        return f"{who} was streaming from {places} at the same time."
    if kind == "new_country":
        where = ", ".join(x for x in (d.get("city"), d.get("country")) if x) or d.get("country_code")
        return f"{who} played from {where} for the first time."
    if kind == "server_down":
        return f"Plex stopped responding ({d.get('error') or 'no answer'})."
    if kind == "server_up":
        return f"Plex came back after {_dur(d.get('down_for_s'))}."
    return kind


def list_alerts(conn: sqlite3.Connection, *, include_acked: bool = False, user_id: int | None = None,
                limit: int = 100) -> list[dict]:
    where, params = [], []
    if not include_acked:
        where.append("a.acknowledged = 0")
    if user_id is not None:
        where.append("a.user_id = ?")
        params.append(user_id)
    rows = conn.execute(
        "SELECT a.*, COALESCE(u.friendly_name, u.username) AS user_name FROM alerts a "
        "LEFT JOIN users u ON u.id = a.user_id"
        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY a.created_at DESC, a.id DESC LIMIT ?",
        params + [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"] or "{}")
        d["label"] = KINDS.get(d["kind"], d["kind"])
        d["text"] = describe(d["kind"], d["user_name"], d["detail"])
        d["severity"] = "ok" if d["kind"] == "server_up" else ("warn" if d["kind"] in ("new_country",) else "alert")
        out.append(d)
    return out


def unacked_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM alerts WHERE acknowledged = 0").fetchone()[0]


def acknowledge(conn: sqlite3.Connection, alert_id: int | None = None) -> int:
    if alert_id is None:
        return conn.execute("UPDATE alerts SET acknowledged = 1 WHERE acknowledged = 0").rowcount
    return conn.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,)).rowcount
