"""Live activity: what's playing now, totals, and stopping a stream."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from ..plex.client import PlexClient, PlexError
from ..plex.models import PlexSession
from . import settings


@dataclass
class ActiveView:
    session: PlexSession
    user_name: str
    user_id: int
    state: str
    started_at: int
    paused_s: int
    view_offset_s: int
    duration_s: int | None
    observed_at: int

    @property
    def progress_pct(self) -> float:
        if not self.duration_s:
            return 0.0
        return min(100.0, self.view_offset_s / self.duration_s * 100)


def list_active(conn: sqlite3.Connection) -> list[ActiveView]:
    rows = conn.execute(
        "SELECT a.*, COALESCE(u.friendly_name, u.username, CAST(a.user_id AS TEXT)) AS user_name "
        "FROM active_sessions a LEFT JOIN users u ON u.id = a.user_id ORDER BY a.started_at"
    ).fetchall()
    out = []
    for r in rows:
        snap = json.loads(r["snapshot"])
        s = PlexSession.from_dict(snap["session"])
        paused = r["paused_s"]
        out.append(ActiveView(
            session=s, user_name=r["user_name"], user_id=r["user_id"], state=r["state"],
            started_at=r["started_at"], paused_s=paused, view_offset_s=r["view_offset_s"] or 0,
            duration_s=r["duration_s"], observed_at=r["last_seen_at"],
        ))
    return out


def totals(active: list[ActiveView]) -> dict:
    t = {"streams": len(active), "transcode": 0, "direct_stream": 0, "direct_play": 0,
         "bandwidth_kbps": 0, "lan_kbps": 0, "wan_kbps": 0}
    for a in active:
        t[a.session.stream.decision] = t.get(a.session.stream.decision, 0) + 1
        bw = a.session.bandwidth_kbps or 0
        t["bandwidth_kbps"] += bw
        t["lan_kbps" if a.session.is_local else "wan_kbps"] += bw
    return t


def plex_client(conn: sqlite3.Connection) -> PlexClient | None:
    url = settings.get(conn, "plex.url")
    token = settings.get(conn, "plex.token")
    if not (url and token):
        return None
    from .plex_setup import client_id
    return PlexClient(url, token, client_id(conn), verify_tls=settings.get(conn, "plex.verify_tls"))


def stop_session(conn: sqlite3.Connection, session_id: str, message: str) -> None:
    row = conn.execute("SELECT 1 FROM active_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if row is None:
        raise LookupError("That stream has already ended")
    client = plex_client(conn)
    if client is None:
        raise PlexError("Plex is not configured")
    client.terminate(session_id, (message or "").strip()[:200] or "Your stream was stopped by the server admin.")


def stop_user_sessions(conn: sqlite3.Connection, user_id: int, message: str = "",
                       client: PlexClient | None = None) -> int:
    """Stop every active stream for one person. Returns how many were stopped."""
    ids = [r[0] for r in conn.execute("SELECT session_id FROM active_sessions WHERE user_id = ?", (user_id,))]
    if not ids:
        return 0
    client = client or plex_client(conn)
    if client is None:
        raise PlexError("Plex is not configured")
    msg = (message or "").strip()[:200] or "Your stream was stopped by the server admin."
    for sid in ids:
        client.terminate(sid, msg)
    return len(ids)


def worker_status(conn: sqlite3.Connection, now: float | None = None) -> dict:
    now = now or time.time()
    hb = settings.get(conn, "worker.heartbeat_at") or 0
    return {
        "alive": bool(hb) and now - hb < 60,
        "heartbeat_at": hb,
        "error": settings.get(conn, "worker.last_error"),
        "plex_configured": settings.plex_configured(conn),
    }
