"""Play history: filtered, grouped by resume, keyset-paginated."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

PAGE_SIZE = 50
MEDIA_TYPES = ("movie", "episode", "track")
DECISIONS = ("direct_play", "direct_stream", "transcode")


@dataclass
class HistoryFilter:
    user_id: int | None = None
    media_type: str | None = None
    decision: str | None = None
    q: str | None = None
    date_from: str | None = None   # YYYY-MM-DD, local time
    date_to: str | None = None
    before: str | None = None      # cursor "started_at:id"

    @classmethod
    def from_args(cls, args) -> "HistoryFilter":
        def _int(v):
            try:
                return int(v) if v not in (None, "") else None
            except ValueError:
                return None

        def _date(v):
            try:
                return datetime.strptime(v, "%Y-%m-%d").strftime("%Y-%m-%d") if v else None
            except ValueError:
                return None

        mt = args.get("type") or None
        dec = args.get("decision") or None
        return cls(
            user_id=_int(args.get("user")),
            media_type=mt if mt in MEDIA_TYPES else None,
            decision=dec if dec in DECISIONS else None,
            q=(args.get("q") or "").strip() or None,
            date_from=_date(args.get("from")),
            date_to=_date(args.get("to")),
            before=args.get("before") or None,
        )

    def query_args(self, **override) -> dict:
        d = {"user": self.user_id, "type": self.media_type, "decision": self.decision,
             "q": self.q, "from": self.date_from, "to": self.date_to}
        d.update(override)
        return {k: v for k, v in d.items() if v not in (None, "")}

    @property
    def active(self) -> bool:
        return bool(self.query_args())


@dataclass
class HistoryRow:
    play: dict
    parts: int
    total_play_s: int
    user_name: str
    reasons: list[str] = field(default_factory=list)


def _where(f: HistoryFilter, tz: str) -> tuple[str, list]:
    clauses = ["p.group_id IS NULL"]
    params: list = []
    if f.user_id is not None:
        clauses.append("p.user_id = ?")
        params.append(f.user_id)
    if f.media_type:
        clauses.append("p.media_type = ?")
        params.append(f.media_type)
    if f.decision:
        clauses.append("p.decision = ?")
        params.append(f.decision)
    if f.q:
        clauses.append("(p.title LIKE ? OR p.grandparent_title LIKE ? OR p.parent_title LIKE ?)")
        like = f"%{f.q}%"
        params += [like, like, like]
    zone = ZoneInfo(tz)
    if f.date_from:
        d = datetime.strptime(f.date_from, "%Y-%m-%d")
        clauses.append("p.started_at >= ?")
        params.append(int(datetime.combine(d, dtime.min, zone).timestamp()))
    if f.date_to:
        d = datetime.strptime(f.date_to, "%Y-%m-%d")
        clauses.append("p.started_at <= ?")
        params.append(int(datetime.combine(d, dtime.max, zone).timestamp()))
    return " AND ".join(clauses), params


def list_history(conn: sqlite3.Connection, f: HistoryFilter, tz: str,
                 limit: int = PAGE_SIZE) -> tuple[list[HistoryRow], str | None]:
    where, params = _where(f, tz)
    if f.before:
        try:
            b_started, b_id = (int(x) for x in f.before.split(":", 1))
            where += " AND (p.started_at < ? OR (p.started_at = ? AND p.id < ?))"
            params += [b_started, b_started, b_id]
        except ValueError:
            pass
    rows = conn.execute(
        f"""
        SELECT p.*, COALESCE(u.friendly_name, u.username, CAST(p.user_id AS TEXT)) AS user_name,
               1 + (SELECT COUNT(*) FROM plays c WHERE c.group_id = p.id) AS parts,
               p.play_s + COALESCE((SELECT SUM(c.play_s) FROM plays c WHERE c.group_id = p.id), 0) AS total_play_s,
               COALESCE((SELECT c.percent_complete FROM plays c WHERE c.group_id = p.id
                         ORDER BY c.started_at DESC LIMIT 1), p.percent_complete) AS last_pct,
               COALESCE((SELECT MAX(c.stopped_at) FROM plays c WHERE c.group_id = p.id), p.stopped_at) AS last_stop,
               MAX(p.watched, COALESCE((SELECT MAX(c.watched) FROM plays c WHERE c.group_id = p.id), 0)) AS any_watched
        FROM plays p LEFT JOIN users u ON u.id = p.user_id
        WHERE {where}
        ORDER BY p.started_at DESC, p.id DESC
        LIMIT ?
        """,
        params + [limit + 1],
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    out = [_row(r) for r in rows]
    cursor = f"{rows[-1]['started_at']}:{rows[-1]['id']}" if more and rows else None
    return out, cursor


def _row(r: sqlite3.Row) -> HistoryRow:
    d = dict(r)
    try:
        reasons = json.loads(d.get("transcode_reasons") or "[]")
    except ValueError:
        reasons = []
    return HistoryRow(play=d, parts=d["parts"], total_play_s=d["total_play_s"],
                      user_name=d["user_name"], reasons=reasons)


def group_parts(conn: sqlite3.Connection, play_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM plays WHERE id = ? OR group_id = ? ORDER BY started_at", (play_id, play_id)
    ).fetchall()
    return [dict(r) for r in rows]


def users_for_filter(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, COALESCE(friendly_name, username, CAST(id AS TEXT)) AS name FROM users "
        "WHERE id IN (SELECT DISTINCT user_id FROM plays) OR is_active = 1 ORDER BY name COLLATE NOCASE"
    )]


CSV_COLUMNS = ["started_at", "stopped_at", "user", "media_type", "show", "season", "episode", "title",
               "year", "play_s", "paused_s", "percent_complete", "watched", "decision", "transcode_reasons",
               "player", "platform", "product", "ip_address", "is_local", "src_resolution",
               "dst_resolution", "bandwidth_kbps"]


def export_csv(conn: sqlite3.Connection, f: HistoryFilter, tz: str) -> str:
    """All plays matching the filter (resume parts included as their own rows)."""
    where, params = _where(f, tz)
    where = where.replace("p.group_id IS NULL", "1=1", 1)
    zone = ZoneInfo(tz)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in conn.execute(
        f"SELECT p.*, COALESCE(u.friendly_name, u.username) AS user_name FROM plays p "
        f"LEFT JOIN users u ON u.id = p.user_id WHERE {where} ORDER BY p.started_at DESC", params
    ):
        fmt = lambda ts: datetime.fromtimestamp(ts, zone).strftime("%Y-%m-%d %H:%M:%S") if ts else ""  # noqa: E731
        w.writerow([fmt(r["started_at"]), fmt(r["stopped_at"]), r["user_name"], r["media_type"],
                    r["grandparent_title"] or "", r["season_index"] or "", r["episode_index"] or "",
                    r["title"], r["year"] or "", r["play_s"], r["paused_s"], r["percent_complete"],
                    r["watched"], r["decision"], ";".join(json.loads(r["transcode_reasons"] or "[]")),
                    r["player_title"], r["platform"], r["product"], r["ip_address"], r["is_local"],
                    r["src_resolution"], r["dst_resolution"], r["bandwidth_kbps"]])
    return buf.getvalue()
