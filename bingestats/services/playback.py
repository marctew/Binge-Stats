"""Playback problems: buffering and slow transcodes (Insights → Playback).

Only plays Binge Stats recorded live count: Tautulli's history has no
buffering data, so imported plays would make every rate look perfect.
Rates are buffer events per 10 hours watched, so a long film with one
stall doesn't look worse than ten short episodes each with one.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from . import stats


def _rows(conn: sqlite3.Connection, rng: stats.Range) -> list:
    return conn.execute(
        "SELECT p.id, p.group_id, p.user_id, p.started_at, p.play_s, p.buffer_count, p.decision, p.transcode_reasons, "
        "p.player_title, p.product, p.is_local, p.ip_address, p.media_type, p.title, p.grandparent_title, "
        "p.season_index, p.episode_index, p.year, p.thumb, p.bandwidth_kbps, p.src_resolution, p.dst_resolution, "
        "COALESCE(u.friendly_name, u.username) AS user_name, g.city, g.country "
        "FROM plays p LEFT JOIN users u ON u.id = p.user_id LEFT JOIN ip_geo g ON g.ip = p.ip_address "
        "WHERE p.source = 'live' AND p.started_at >= ? AND p.started_at < ? ORDER BY p.started_at",
        (rng.start, rng.end)).fetchall()


def _slow(r) -> bool:
    try:
        return "transcoder_slow" in json.loads(r["transcode_reasons"] or "[]")
    except (TypeError, ValueError):
        return False


def _place(r) -> str:
    if r["is_local"]:
        return "Home network"
    if r["city"] or r["country"]:
        return ", ".join(x for x in (r["city"], r["country"]) if x)
    return "Remote"


def _rate(events: int, play_s: int) -> float:
    return round(events / (play_s / 36000), 1) if play_s >= 600 else 0.0


def summary(conn: sqlite3.Connection, rng: stats.Range) -> dict:
    rows = _rows(conn, rng)
    play_s = sum(r["play_s"] or 0 for r in rows)
    events = sum(r["buffer_count"] or 0 for r in rows)
    heads = [r for r in rows if r["group_id"] is None]
    first = conn.execute("SELECT MIN(started_at) FROM plays WHERE source = 'live'").fetchone()[0]
    return {"plays": len(heads), "buffered": sum(1 for r in rows if (r["buffer_count"] or 0) > 0),
            "events": events, "play_s": play_s, "per_10h": _rate(events, play_s),
            "slow": sum(1 for r in rows if _slow(r)), "since": first}


def breakdown(conn: sqlite3.Connection, rng: stats.Range, by: str, limit: int = 8) -> list[dict]:
    key = {"user": lambda r: r["user_name"] or str(r["user_id"]),
           "device": lambda r: " · ".join(x for x in (r["player_title"], r["product"]) if x) or "Unknown",
           "place": _place,
           "decision": lambda r: stats.DECISION_LABELS.get(r["decision"], "Unknown")}.get(by)
    if key is None:
        raise ValueError(f"Unknown breakdown: {by}")
    acc: dict[str, dict] = defaultdict(lambda: {"plays": 0, "buffered": 0, "events": 0, "play_s": 0, "slow": 0})
    for r in _rows(conn, rng):
        a = acc[key(r)]
        a["plays"] += 1 if r["group_id"] is None else 0
        a["buffered"] += 1 if (r["buffer_count"] or 0) > 0 else 0
        a["events"] += r["buffer_count"] or 0
        a["play_s"] += r["play_s"] or 0
        a["slow"] += 1 if _slow(r) else 0
    out = [{"name": k, **v, "per_10h": _rate(v["events"], v["play_s"])} for k, v in acc.items()]
    out.sort(key=lambda x: (-x["events"], -x["slow"], -x["per_10h"], -x["plays"]))
    return [x for x in out if x["events"] or x["slow"]][:limit]


def worst(conn: sqlite3.Connection, rng: stats.Range, limit: int = 25) -> list[dict]:
    out = []
    for r in _rows(conn, rng):
        if not (r["buffer_count"] or _slow(r)):
            continue
        title = r["grandparent_title"] if r["media_type"] == "episode" and r["grandparent_title"] else r["title"]
        sub = (f"S{r['season_index']:02d}E{r['episode_index']:02d}" if r["media_type"] == "episode"
               and r["season_index"] is not None and r["episode_index"] is not None else (r["year"] or ""))
        out.append({"id": r["id"], "title": title, "sub": sub, "thumb": r["thumb"], "user_id": r["user_id"],
                    "user_name": r["user_name"], "started_at": r["started_at"], "play_s": r["play_s"],
                    "buffer_count": r["buffer_count"] or 0, "slow": _slow(r), "decision": r["decision"],
                    "device": r["player_title"] or r["product"], "place": _place(r),
                    "bandwidth_kbps": r["bandwidth_kbps"], "src_resolution": r["src_resolution"],
                    "dst_resolution": r["dst_resolution"]})
    out.sort(key=lambda x: (-x["buffer_count"], -x["started_at"]))
    return out[:limit]
