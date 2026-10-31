"""Stream limits and sharing detection (SPEC.md section 10).

Called by the poller inside its write transaction when a stream starts or
switches to transcoding. It records alerts, queues notification events and
returns the sessions to stop; the poller stops them after committing, so a
slow Plex call never holds the database lock.

- Stream limit: more active streams than `users.max_streams` -> alert; stop the
  newest if `limit_action` is 'stop'.
- Transcode limit: same, counting only transcoding streams.
- Concurrent IPs: two or more streams on one account from different *public*
  IPs at once. Flag only, never stops anything.
- New country: the first ever play from a country this user hasn't played
  from before (needs at least one earlier located play). Flag only.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..plex.models import PlexSession
from ..services import geo

DEFAULT_STOP_MESSAGE = "You've reached the stream limit on this server."
REALERT_S = 3600   # don't repeat a concurrent-IP alert for the same user within this


def _alert(conn: sqlite3.Connection, now: int, kind: str, user_id: int, detail: dict) -> None:
    conn.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (?, ?, ?, ?)",
                 (now, kind, user_id, json.dumps(detail)))


def _sessions(conn: sqlite3.Connection, user_id: int, live: set[str] | None) -> list[PlexSession]:
    """The user's streams Plex is reporting right now. `live` is this poll's
    session ids: a stream that just ended is still in active_sessions for the
    grace polls and mustn't count, or swapping devices would trip the limit."""
    rows = conn.execute("SELECT session_id, snapshot FROM active_sessions WHERE user_id = ? "
                        "ORDER BY started_at, session_id", (user_id,))
    return [PlexSession.from_dict(json.loads(r["snapshot"])["session"]) for r in rows
            if live is None or r["session_id"] in live]


def _brief(s: PlexSession) -> dict:
    title = s.grandparent_title or s.title
    return {"session_id": s.session_id, "title": title, "player": s.player_title or s.product,
            "ip": s.ip_address, "local": s.is_local, "decision": s.stream.decision}


def _limits(conn: sqlite3.Connection, user_id: int):
    return conn.execute("SELECT max_streams, max_transcodes, limit_action, limit_message FROM users WHERE id = ?",
                        (user_id,)).fetchone()


def on_start(conn: sqlite3.Connection, s: PlexSession, now: int, data_dir: Path,
             events: list, live: set[str] | None = None) -> list[tuple[str, str]]:
    stops: list[tuple[str, str]] = []
    lim = _limits(conn, s.user_id)
    active = _sessions(conn, s.user_id, live)
    if lim is not None and lim["max_streams"] and len(active) > lim["max_streams"]:
        stop = lim["limit_action"] == "stop"
        detail = {"count": len(active), "limit": lim["max_streams"], "sessions": [_brief(x) for x in active],
                  "stopped": s.session_id if stop else None}
        _alert(conn, now, "stream_limit", s.user_id, detail)
        events.append(("stream_limit", {"session": s.to_dict(), "detail": detail}))
        if stop:
            stops.append((s.session_id, lim["limit_message"] or DEFAULT_STOP_MESSAGE))
    if s.stream.decision == "transcode":
        stops += on_transcode(conn, s, now, events, active=active, lim=lim)
    _concurrent_ips(conn, s, now, active, data_dir, events)
    _new_country(conn, s, now, data_dir, events)
    return stops


def on_transcode(conn: sqlite3.Connection, s: PlexSession, now: int, events: list, *,
                 active: list[PlexSession] | None = None, lim=None,
                 live: set[str] | None = None) -> list[tuple[str, str]]:
    lim = lim if lim is not None else _limits(conn, s.user_id)
    if lim is None or not lim["max_transcodes"]:
        return []
    active = active if active is not None else _sessions(conn, s.user_id, live)
    transcoding = [x for x in active if x.stream.decision == "transcode" or x.session_id == s.session_id]
    if len(transcoding) <= lim["max_transcodes"]:
        return []
    stop = lim["limit_action"] == "stop"
    detail = {"count": len(transcoding), "limit": lim["max_transcodes"],
              "sessions": [_brief(x) for x in transcoding], "stopped": s.session_id if stop else None}
    _alert(conn, now, "transcode_limit", s.user_id, detail)
    events.append(("transcode_limit", {"session": s.to_dict(), "detail": detail}))
    return [(s.session_id, lim["limit_message"] or DEFAULT_STOP_MESSAGE)] if stop else []


def _public(s: PlexSession) -> bool:
    return bool(s.ip_address) and not s.is_local and not geo.is_private(s.ip_address)


def _concurrent_ips(conn, s: PlexSession, now: int, active: list[PlexSession], data_dir: Path, events: list) -> None:
    if not _public(s):
        return
    ips = {x.ip_address for x in active if _public(x)}
    if len(ips) < 2:
        return
    recent = conn.execute("SELECT 1 FROM alerts WHERE kind = 'concurrent_ips' AND user_id = ? AND created_at > ?",
                          (s.user_id, now - REALERT_S)).fetchone()
    if recent:
        return
    places = []
    for ip in sorted(ips):
        g = geo.lookup(conn, data_dir, ip, now) or {}
        places.append({"ip": ip, "place": ", ".join(x for x in (g.get("city"), g.get("country")) if x) or "unknown"})
    detail = {"ips": sorted(ips), "places": places,
              "sessions": [_brief(x) for x in active if _public(x)]}
    _alert(conn, now, "concurrent_ips", s.user_id, detail)
    events.append(("concurrent_ips", {"session": s.to_dict(), "detail": detail}))


def _new_country(conn, s: PlexSession, now: int, data_dir: Path, events: list) -> None:
    if not _public(s):
        return
    g = geo.lookup(conn, data_dir, s.ip_address, now)
    if not g or not g.get("country_code"):
        return
    seen = {r[0] for r in conn.execute(
        "SELECT DISTINCT g.country_code FROM plays p JOIN ip_geo g ON g.ip = p.ip_address "
        "WHERE p.user_id = ? AND g.country_code IS NOT NULL", (s.user_id,))}
    if not seen or g["country_code"] in seen:
        return
    already = conn.execute("SELECT 1 FROM alerts WHERE kind = 'new_country' AND user_id = ? "
                           "AND json_extract(detail, '$.country_code') = ?", (s.user_id, g["country_code"])).fetchone()
    if already:
        return
    detail = {"country": g.get("country"), "country_code": g["country_code"], "city": g.get("city"),
              "ip": s.ip_address, "usual": sorted(seen)}
    _alert(conn, now, "new_country", s.user_id, detail)
    events.append(("new_country", {"session": s.to_dict(), "detail": detail}))
