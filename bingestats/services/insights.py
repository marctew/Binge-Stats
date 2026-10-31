"""Transcode insights and library health (SPEC.md section 12)."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from collections import Counter, defaultdict

from ..transcode import FILE_REASONS, REASON_LABELS
from . import settings, stats
from .binges import show_guids, show_key

# -- transcodes --------------------------------------------------------------------------

BY_FIELDS = {
    "user": "Person",
    "product": "App",
    "player": "Device",
    "src_video_codec": "Source video codec",
    "src_audio_codec": "Source audio codec",
    "src_resolution": "Source resolution",
}
CANDIDATE_MIN_PLAYS = 3
CANDIDATE_MIN_SHARE = 0.5


def _heads(conn: sqlite3.Connection, rng: stats.Range | None, extra_where: str = "", params=()) -> list:
    """One row per play (resumed parts folded into their first part)."""
    where = ["p.group_id IS NULL"]
    args: list = []
    if rng is not None:
        where += ["p.started_at >= ?", "p.started_at < ?"]
        args += [rng.start, rng.end]
    if extra_where:
        where.append(extra_where)
        args += list(params)
    return conn.execute(
        "SELECT p.id, p.user_id, p.guid, p.rating_key, p.media_type, p.title, p.grandparent_title, p.year, "
        "p.season_index, p.episode_index, p.thumb, p.decision, p.transcode_reasons, p.product, p.player_title, "
        "p.src_video_codec, p.src_audio_codec, p.src_audio_channels, p.src_resolution, p.src_container, "
        "p.src_dynamic_range, p.started_at, COALESCE(u.friendly_name, u.username) AS user_name "
        f"FROM plays p LEFT JOIN users u ON u.id = p.user_id WHERE {' AND '.join(where)}", args).fetchall()


def _reasons(row) -> list[str]:
    try:
        return list(json.loads(row["transcode_reasons"] or "[]"))
    except (TypeError, ValueError):
        return []


def decision_share(conn: sqlite3.Connection, rng: stats.Range) -> dict:
    """Share of plays by decision per period, as percentages (stacked to 100)."""
    ts = stats.timeseries(conn, rng, metric="plays", split="decision")
    totals = [sum(col) for col in zip(*(s["data"] for s in ts["series"]))] if ts["series"] else []
    for s in ts["series"]:
        s["data"] = [round(v / t * 100, 1) if t else 0 for v, t in zip(s["data"], totals)]
    order = {"Direct Play": 0, "Direct Stream": 1, "Transcode": 2}
    ts["series"].sort(key=lambda s: order.get(s["name"], 9))
    ts["counts"] = totals
    return ts


def reasons(conn: sqlite3.Connection, rng: stats.Range) -> dict:
    """Why things transcoded, ranked. A play can have several reasons."""
    c: Counter = Counter()
    for r in _heads(conn, rng, "p.decision = 'transcode'"):
        rs = _reasons(r)
        for code in rs or ["unknown"]:
            c[REASON_LABELS.get(code, "Not recorded" if code == "unknown" else code)] += 1
    return {"items": [{"name": n, "value": v} for n, v in c.most_common()]}


def _label(field: str, row) -> str:
    if field == "user":
        return row["user_name"] or str(row["user_id"])
    if field == "player":
        return row["player_title"] or "Unknown"
    if field == "src_resolution":
        return stats._res_label(row["src_resolution"])
    v = row[field]
    return (str(v).upper() if field != "product" else str(v)) if v else "Unknown"


def transcode_by(conn: sqlite3.Connection, rng: stats.Range, field: str, limit: int = 8) -> list[dict]:
    """For each person / app / codec: plays, transcodes and the transcoded share."""
    field = field if field in BY_FIELDS else "user"
    plays: Counter = Counter()
    trans: Counter = Counter()
    for r in _heads(conn, rng):
        name = _label(field, r)
        plays[name] += 1
        if r["decision"] == "transcode":
            trans[name] += 1
    rows = [{"name": n, "plays": plays[n], "transcodes": trans[n],
             "pct": round(trans[n] / plays[n] * 100) if plays[n] else 0} for n in plays]
    rows.sort(key=lambda x: (-x["transcodes"], -x["pct"], -x["plays"]))
    return [r for r in rows if r["transcodes"]][:limit]


def _display(row) -> str:
    if row["media_type"] == "episode" and row["grandparent_title"]:
        se = ""
        if row["season_index"] is not None and row["episode_index"] is not None:
            se = f" S{row['season_index']:02d}E{row['episode_index']:02d}"
        return f"{row['grandparent_title']}{se}"
    return f"{row['title']} ({row['year']})" if row["year"] else (row["title"] or "Unknown")


def reencode_candidates(conn: sqlite3.Connection, rng: stats.Range | None = None, limit: int = 50) -> list[dict]:
    """Files that keep transcoding because of the file itself (codec, HDR,
    subtitles, container): >= 3 plays and >= half of them transcoded for one
    of those reasons. Ranked by transcoded plays."""
    acc: dict[str, dict] = {}
    for r in _heads(conn, rng, "p.media_type IN ('movie', 'episode') AND p.guid IS NOT NULL"):
        a = acc.setdefault(r["guid"], {"guid": r["guid"], "plays": 0, "file_transcodes": 0, "reasons": Counter(),
                                       "people": set(), "last": r})
        a["plays"] += 1
        a["people"].add(r["user_id"])
        if r["started_at"] >= a["last"]["started_at"]:
            a["last"] = r
        file_reasons = [x for x in _reasons(r) if x in FILE_REASONS] if r["decision"] == "transcode" else []
        if file_reasons:
            a["file_transcodes"] += 1
            a["reasons"].update(file_reasons)
    picked = [a for a in acc.values()
              if a["plays"] >= CANDIDATE_MIN_PLAYS and a["file_transcodes"] / a["plays"] >= CANDIDATE_MIN_SHARE]
    picked.sort(key=lambda a: (-a["file_transcodes"], -a["plays"]))
    picked = picked[:limit]
    files = {}
    if picked:
        g = [a["guid"] for a in picked]
        for m in conn.execute(
                "SELECT guid, rating_key, video_codec, video_resolution, video_dynamic_range, audio_codec, "
                "audio_channels, container, size_bytes, thumb, grandparent_rating_key FROM media_items "
                f"WHERE is_present = 1 AND guid IN ({','.join('?' * len(g))})", g):
            files[m["guid"]] = m
    out = []
    for a in picked:
        last, m = a["last"], files.get(a["guid"])
        out.append({
            "title": _display(last), "media_type": last["media_type"], "guid": a["guid"],
            "rating_key": m["rating_key"] if m else None, "thumb": (m["thumb"] if m else None) or last["thumb"],
            "plays": a["plays"], "file_transcodes": a["file_transcodes"], "people": len(a["people"]),
            "pct": round(a["file_transcodes"] / a["plays"] * 100),
            "reasons": [REASON_LABELS.get(k, k) for k, _n in a["reasons"].most_common()],
            "video_codec": (m["video_codec"] if m else None) or last["src_video_codec"],
            "resolution": (m["video_resolution"] if m else None) or last["src_resolution"],
            "dynamic_range": (m["video_dynamic_range"] if m else None) or last["src_dynamic_range"],
            "audio_codec": (m["audio_codec"] if m else None) or last["src_audio_codec"],
            "audio_channels": (m["audio_channels"] if m else None) or last["src_audio_channels"],
            "container": (m["container"] if m else None) or last["src_container"],
            "size_bytes": m["size_bytes"] if m else None, "in_library": m is not None,
        })
    return out


def transcode_summary(conn: sqlite3.Connection, rng: stats.Range) -> dict:
    heads = _heads(conn, rng)
    t = [r for r in heads if r["decision"] == "transcode"]
    file_t = [r for r in t if any(x in FILE_REASONS for x in _reasons(r))]
    return {"plays": len(heads), "transcodes": len(t),
            "pct": round(len(t) / len(heads) * 100) if heads else 0,
            "file_transcodes": len(file_t),
            "file_pct": round(len(file_t) / len(t) * 100) if t else 0,
            "people": len({r["user_id"] for r in t})}


# -- library health ----------------------------------------------------------------------

TABS = {"stale": "Stale", "never": "Never watched", "cost": "GB per hour watched"}
NEVER_DAYS = 90
MONTH_S = 30.44 * 86400


def _items(conn: sqlite3.Connection, library_id: int | None, kind: str | None) -> list[dict]:
    where = ["m.is_present = 1", "m.type IN ('movie', 'show')"]
    params: list = []
    if library_id is not None:
        where.append("m.library_id = ?")
        params.append(library_id)
    if kind in ("movie", "show"):
        where.append("m.type = ?")
        params.append(kind)
    items = [dict(r) for r in conn.execute(
        "SELECT m.rating_key, m.guid, m.type, m.title, m.year, m.thumb, m.added_at, m.size_bytes, m.library_id, "
        "l.name AS library_name FROM media_items m LEFT JOIN libraries l ON l.id = m.library_id "
        f"WHERE {' AND '.join(where)}", params)]
    shows = [i["rating_key"] for i in items if i["type"] == "show"]
    if shows:
        sizes = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT grandparent_rating_key, SUM(size_bytes), COUNT(*), MAX(added_at) FROM media_items "
            "WHERE type = 'episode' AND is_present = 1 GROUP BY grandparent_rating_key")}
        for i in items:
            if i["type"] == "show":
                size, n, newest = sizes.get(i["rating_key"], (0, 0, None))
                i.update(size_bytes=size or 0, episodes=n, newest_added=newest)
    return items


def _play_stats(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """Per movie guid and per show key: plays, hours, last played and by whom."""
    guids = show_guids(conn)
    movies: dict[str, dict] = defaultdict(lambda: {"plays": 0, "play_s": 0, "last_played": None, "last_user": None})
    shows: dict[str, dict] = defaultdict(lambda: {"plays": 0, "play_s": 0, "last_played": None, "last_user": None})
    for r in conn.execute(
            "SELECT p.media_type, p.guid, p.grandparent_guid, p.grandparent_rating_key, p.grandparent_title, "
            "p.group_id, p.play_s, p.started_at, COALESCE(u.friendly_name, u.username) AS user_name "
            "FROM plays p LEFT JOIN users u ON u.id = p.user_id WHERE p.media_type IN ('movie', 'episode') "
            "ORDER BY p.started_at"):
        if r["media_type"] == "movie":
            key, bucket = r["guid"], movies
        else:
            key, bucket = show_key(r, guids), shows
        if not key:
            continue
        s = bucket[key]
        s["plays"] += 1 if r["group_id"] is None else 0
        s["play_s"] += r["play_s"] or 0
        s["last_played"], s["last_user"] = r["started_at"], r["user_name"]
    return movies, shows


def library_health(conn: sqlite3.Connection, *, library_id: int | None = None, kind: str | None = None,
                   now: float | None = None) -> dict:
    now = now or time.time()
    months = int(settings.get(conn, "library.stale_months"))
    cutoff = now - months * MONTH_S
    never_cutoff = now - NEVER_DAYS * 86400
    movies, shows = _play_stats(conn)
    rows = []
    for i in _items(conn, library_id, kind):
        s = (movies if i["type"] == "movie" else shows).get(i["guid"]) if i["guid"] else None
        if s is None and i["type"] == "show":
            s = shows.get(f"rk:{i['rating_key']}")
        s = s or {"plays": 0, "play_s": 0, "last_played": None, "last_user": None}
        hours = s["play_s"] / 3600
        size_gb = (i["size_bytes"] or 0) / 1e9
        rows.append({**i, **s, "hours": round(hours, 1),
                     "gb_per_hour": round(size_gb / hours, 1) if hours >= 0.05 else None})
    added = lambda r: r.get("newest_added") or r["added_at"] or 0  # noqa: E731 - a show counts from its newest episode
    stale = [r for r in rows if added(r) < cutoff and (r["last_played"] or 0) < cutoff]
    never = [r for r in rows if added(r) < never_cutoff and not r["plays"]]
    cost = [r for r in rows if r["size_bytes"]]
    by_size = lambda r: -(r["size_bytes"] or 0)  # noqa: E731
    stale.sort(key=by_size)
    never.sort(key=by_size)
    # no plays = infinite cost: those first (biggest first), then the worst ratio
    cost.sort(key=lambda r: (r["gb_per_hour"] is not None, -(r["gb_per_hour"] or 0), by_size(r)))
    return {
        "months": months, "never_days": NEVER_DAYS,
        "stale": stale, "never": never, "cost": cost,
        "totals": {
            "stale_bytes": sum(r["size_bytes"] or 0 for r in stale), "stale_items": len(stale),
            "never_bytes": sum(r["size_bytes"] or 0 for r in never), "never_items": len(never),
            "item_count": len(rows), "bytes": sum(r["size_bytes"] or 0 for r in rows),
        },
    }


def health_csv(rows: list[dict], tz) -> str:
    from datetime import datetime
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["title", "year", "type", "library", "size_gb", "added", "plays", "hours_watched", "gb_per_hour",
                "last_played", "last_played_by"])
    fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d") if ts else ""  # noqa: E731
    for r in rows:
        w.writerow([r["title"], r["year"] or "", r["type"], r["library_name"] or "",
                    round((r["size_bytes"] or 0) / 1e9, 2), fmt(r.get("newest_added") or r["added_at"]), r["plays"],
                    r["hours"], "" if r["gb_per_hour"] is None else r["gb_per_hour"], fmt(r["last_played"]),
                    r["last_user"] or ""])
    return out.getvalue()
