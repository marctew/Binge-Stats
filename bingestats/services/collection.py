"""Collection: what the library is made of, and where it could be better,
without deleting anything (Insights → Collection).

- Make-up: size and counts by video codec, resolution and audio codec.
- Re-encode for space: the biggest items still in older codecs (H.264,
  MPEG-2, VC-1), with an estimated saving from re-encoding to HEVC/AV1.
- Upgrade candidates: films, and shows by season, below 1080p.
- Gaps in shows: missing episode numbers inside a season you have, and
  missing seasons between ones you have. Plex only knows what's on disk, so
  episodes after the last one you have can't be seen from here.

Counts and sizes are of films and episodes in the library (is_present = 1).
"""
from __future__ import annotations

import csv
import io
import sqlite3
from collections import defaultdict

# Re-encoding H.264 to HEVC at the same quality typically lands at roughly half
# the size; older codecs save more. A planning estimate, not a promise.
SAVING = {"h264": 0.5, "mpeg2video": 0.75, "vc1": 0.6, "mpeg4": 0.6, "msmpeg4v3": 0.6, "wmv3": 0.6}
LOW_RES = ("sd", "480", "576", "720")
RES_ORDER = ["8k", "4k", "2k", "1080", "720", "576", "480", "sd"]
# Seasons numbered like 2024, or with episode numbers past this, are date-based
# (news, sport, YouTube): numbering gaps there aren't missing episodes.
MAX_EPISODE_INDEX = 150
MAX_SEASON_FOR_GAPS = 200


def _scope(library_id: int | None, kind: str | None, alias: str = "m") -> tuple[str, list]:
    where = [f"{alias}.is_present = 1"]
    params: list = []
    if kind == "movie":
        where.append(f"{alias}.type = 'movie'")
    elif kind == "show":
        where.append(f"{alias}.type = 'episode'")
    else:
        where.append(f"{alias}.type IN ('movie', 'episode')")
    if library_id is not None:
        where.append(f"{alias}.library_id = ?")
        params.append(library_id)
    return " AND ".join(where), params


def _norm_res(v) -> str:
    v = str(v or "").lower()
    return v if v in RES_ORDER else ("sd" if v in ("", "none") else v)


def makeup(conn: sqlite3.Connection, library_id: int | None = None, kind: str | None = None) -> dict:
    where, params = _scope(library_id, kind)
    rows = conn.execute(f"SELECT m.video_codec, m.video_resolution, m.audio_codec, m.size_bytes FROM media_items m "
                        f"WHERE {where}", params).fetchall()
    by_codec, by_res, by_audio = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    total = saving = old = low = 0
    for r in rows:
        size = r["size_bytes"] or 0
        total += size
        vc = (r["video_codec"] or "unknown").lower()
        res = _norm_res(r["video_resolution"])
        for bucket, key in ((by_codec, vc), (by_res, res), (by_audio, (r["audio_codec"] or "unknown").lower())):
            bucket[key][0] += 1
            bucket[key][1] += size
        if vc in SAVING:
            old += size
            saving += size * SAVING[vc]
        if res in LOW_RES:
            low += 1

    def ranked(d, order=None):
        items = [{"key": k, "files": v[0], "bytes": v[1]} for k, v in d.items()]
        if order:
            items.sort(key=lambda x: order.index(x["key"]) if x["key"] in order else len(order))
        else:
            items.sort(key=lambda x: -x["bytes"])
        return items

    return {"files": len(rows), "bytes": total, "old_codec_bytes": old, "saving_bytes": int(saving),
            "low_res_items": low, "codecs": ranked(by_codec), "resolutions": ranked(by_res, RES_ORDER),
            "audio": ranked(by_audio)}


def reencode(conn: sqlite3.Connection, library_id: int | None = None, kind: str | None = None,
             limit: int | None = 100) -> list[dict]:
    """Biggest wins first. Films one by one; shows rolled up per show."""
    where, params = _scope(library_id, kind)
    codecs = tuple(SAVING)
    rows = conn.execute(
        f"SELECT m.rating_key, m.type, m.title, m.year, m.thumb, m.video_codec, m.video_resolution, m.size_bytes, "
        f"m.grandparent_rating_key, m.grandparent_title, l.name AS library_name FROM media_items m "
        f"LEFT JOIN libraries l ON l.id = m.library_id WHERE {where} AND lower(m.video_codec) IN "
        f"({','.join('?' * len(codecs))})", params + list(codecs)).fetchall()
    shows: dict[int, dict] = {}
    out = []
    for r in rows:
        size = r["size_bytes"] or 0
        save = size * SAVING[(r["video_codec"] or "").lower()]
        if r["type"] == "movie":
            out.append({"rating_key": r["rating_key"], "type": "movie", "title": r["title"], "year": r["year"],
                        "thumb": r["thumb"], "codec": r["video_codec"], "resolution": r["video_resolution"],
                        "files": 1, "bytes": size, "saving": int(save), "library_name": r["library_name"]})
        else:
            s = shows.setdefault(r["grandparent_rating_key"], {
                "rating_key": r["grandparent_rating_key"], "type": "show", "title": r["grandparent_title"],
                "year": None, "thumb": None, "codecs": set(), "resolutions": set(), "files": 0, "bytes": 0,
                "saving": 0.0, "library_name": r["library_name"]})
            s["codecs"].add(r["video_codec"])
            s["resolutions"].add(_norm_res(r["video_resolution"]))
            s["files"] += 1
            s["bytes"] += size
            s["saving"] += save
    if shows:
        keys = [k for k in shows if k is not None]
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            for m in conn.execute(f"SELECT rating_key, title, thumb, year FROM media_items WHERE rating_key IN "
                                  f"({','.join('?' * len(chunk))})", chunk):
                shows[m["rating_key"]].update(thumb=m["thumb"], year=m["year"], title=m["title"] or shows[m["rating_key"]]["title"])
    for s in shows.values():
        codecs, resolutions = s.pop("codecs"), s.pop("resolutions")
        out.append({**s, "codec": ", ".join(sorted(c for c in codecs if c)),
                    "resolution": "/".join(sorted(resolutions, key=lambda x: RES_ORDER.index(x) if x in RES_ORDER else 99)),
                    "saving": int(s["saving"])})
    out.sort(key=lambda x: -x["saving"])
    return out[:limit] if limit else out


def upgrades(conn: sqlite3.Connection, library_id: int | None = None, kind: str | None = None,
             limit: int | None = 100) -> list[dict]:
    """Below 1080p: films one by one, shows by season. Most-played first, then biggest."""
    where, params = _scope(library_id, kind)
    rows = conn.execute(
        f"SELECT m.rating_key, m.type, m.title, m.year, m.thumb, m.guid, m.video_resolution, m.size_bytes, "
        f"m.grandparent_rating_key, m.grandparent_title, m.season_index, l.name AS library_name FROM media_items m "
        f"LEFT JOIN libraries l ON l.id = m.library_id WHERE {where} AND lower(COALESCE(m.video_resolution, 'sd')) "
        f"IN ('sd', '480', '576', '720')", params).fetchall()
    plays = {r[0]: r[1] for r in conn.execute(
        "SELECT guid, COUNT(*) FROM plays WHERE group_id IS NULL AND guid IS NOT NULL GROUP BY guid")}
    films, seasons = [], {}
    for r in rows:
        if r["type"] == "movie":
            films.append({"rating_key": r["rating_key"], "type": "movie", "title": r["title"], "year": r["year"],
                          "thumb": r["thumb"], "resolution": _norm_res(r["video_resolution"]), "files": 1,
                          "bytes": r["size_bytes"] or 0, "plays": plays.get(r["guid"], 0),
                          "library_name": r["library_name"]})
        else:
            key = (r["grandparent_rating_key"], r["season_index"])
            s = seasons.setdefault(key, {"rating_key": r["grandparent_rating_key"], "type": "season",
                                         "title": r["grandparent_title"], "season": r["season_index"], "year": None,
                                         "thumb": None, "resolutions": set(), "files": 0, "bytes": 0, "plays": 0,
                                         "library_name": r["library_name"]})
            s["resolutions"].add(_norm_res(r["video_resolution"]))
            s["files"] += 1
            s["bytes"] += r["size_bytes"] or 0
            s["plays"] += plays.get(r["guid"], 0)
    show_rows = {}
    keys = [k for k in {s["rating_key"] for s in seasons.values()} if k is not None]
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        for m in conn.execute(f"SELECT rating_key, title, thumb, year FROM media_items WHERE rating_key IN "
                              f"({','.join('?' * len(chunk))})", chunk):
            show_rows[m["rating_key"]] = m
    for s in seasons.values():
        m = show_rows.get(s["rating_key"])
        if m is not None:
            s.update(title=m["title"] or s["title"], thumb=m["thumb"], year=m["year"])
        s["resolution"] = "/".join(sorted(s.pop("resolutions"), key=lambda x: RES_ORDER.index(x) if x in RES_ORDER else 99))
    out = films + list(seasons.values())
    out.sort(key=lambda x: (-x["plays"], -x["bytes"]))
    return out[:limit] if limit else out


def _ranges(nums: list[int]) -> str:
    """[4, 7, 8, 9] -> 'E04, E07–E09'"""
    parts, start, prev = [], None, None
    for n in nums + [None]:
        if start is None:
            start = prev = n
        elif n is not None and n == prev + 1:
            prev = n
        else:
            parts.append(f"E{start:02d}" if start == prev else f"E{start:02d}–E{prev:02d}")
            start = prev = n
    return ", ".join(parts)


def gaps(conn: sqlite3.Connection, library_id: int | None = None, limit: int | None = None) -> list[dict]:
    """Shows with holes: missing episode numbers inside seasons, and missing seasons between ones you have."""
    where, params = _scope(library_id, "show")
    eps: dict[int, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    for r in conn.execute(f"SELECT m.grandparent_rating_key, m.season_index, m.episode_index FROM media_items m "
                          f"WHERE {where} AND m.season_index IS NOT NULL AND m.episode_index IS NOT NULL", params):
        eps[r[0]][r[1]].add(r[2])
    shows = {r["rating_key"]: r for r in conn.execute(
        "SELECT rating_key, title, year, thumb, library_id FROM media_items WHERE type = 'show' AND is_present = 1")}
    out = []
    for show_rk, seasons in eps.items():
        real = sorted(s for s in seasons if 1 <= s < MAX_SEASON_FOR_GAPS)
        if not real:
            continue
        missing_seasons = [s for s in range(real[0], real[-1] + 1) if s not in seasons]
        holes = []
        for s in real:
            have = seasons[s]
            top = max(have)
            if top > MAX_EPISODE_INDEX:
                continue
            missing = [e for e in range(1, top + 1) if e not in have]
            if missing:
                holes.append({"season": s, "missing": missing, "text": _ranges(missing), "have": len(have), "of": top})
        if not holes and not missing_seasons:
            continue
        info = shows.get(show_rk)
        out.append({"rating_key": show_rk, "title": info["title"] if info else f"Show {show_rk}",
                    "year": info["year"] if info else None, "thumb": info["thumb"] if info else None,
                    "missing_seasons": missing_seasons, "holes": holes,
                    "missing_episodes": sum(len(h["missing"]) for h in holes)})
    out.sort(key=lambda x: (-(len(x["missing_seasons"]) * 10 + x["missing_episodes"]), x["title"] or ""))
    return out[:limit] if limit else out


def to_csv(kind: str, rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    if kind == "gaps":
        w.writerow(["show", "year", "missing_seasons", "missing_episodes", "detail"])
        for r in rows:
            detail = "; ".join(f"S{h['season']:02d}: {h['text']}" for h in r["holes"])
            w.writerow([r["title"], r["year"] or "", " ".join(str(s) for s in r["missing_seasons"]),
                        r["missing_episodes"], detail])
        return out.getvalue()
    w.writerow(["title", "year", "type", "season", "library", "resolution", "codec", "files", "size_gb",
                "estimated_saving_gb" if kind == "reencode" else "plays"])
    for r in rows:
        w.writerow([r["title"], r.get("year") or "", r["type"], r.get("season") if r.get("season") is not None else "",
                    r.get("library_name") or "", r.get("resolution") or "", r.get("codec") or "", r["files"],
                    round(r["bytes"] / 1e9, 2),
                    round(r["saving"] / 1e9, 2) if kind == "reencode" else r.get("plays", 0)])
    return out.getvalue()
