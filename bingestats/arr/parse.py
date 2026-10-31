"""Turn Sonarr/Radarr/SABnzbd JSON into plain rows for the snapshot tables."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def ts(value) -> int | None:
    """ISO 8601 ('2026-10-31T01:00:00Z') -> epoch seconds."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


def series(s: dict) -> dict:
    st = s.get("statistics") or {}
    seasons = [{"season": x.get("seasonNumber"), "monitored": bool(x.get("monitored")),
                "files": (x.get("statistics") or {}).get("episodeFileCount", 0),
                "episodes": (x.get("statistics") or {}).get("episodeCount", 0),
                "total": (x.get("statistics") or {}).get("totalEpisodeCount", 0)} for x in s.get("seasons") or []]
    return {"id": s["id"], "title": s.get("title"), "year": s.get("year") or None, "tvdb_id": s.get("tvdbId") or None,
            "imdb_id": s.get("imdbId") or None, "status": s.get("status"), "ended": int(bool(s.get("ended"))),
            "monitored": int(bool(s.get("monitored"))), "size_bytes": st.get("sizeOnDisk") or 0,
            "episode_count": st.get("episodeCount") or 0, "episode_file_count": st.get("episodeFileCount") or 0,
            "total_episode_count": st.get("totalEpisodeCount") or 0, "seasons": json.dumps(seasons),
            "previous_airing": ts(s.get("previousAiring")), "next_airing": ts(s.get("nextAiring")),
            "added_at": ts(s.get("added"))}


def episode(e: dict) -> dict:
    return {"series_id": e.get("seriesId"), "season": e.get("seasonNumber"), "episode": e.get("episodeNumber"),
            "title": e.get("title"), "air_at": ts(e.get("airDateUtc")), "has_file": int(bool(e.get("hasFile"))),
            "monitored": int(bool(e.get("monitored")))}


def episode_cutoff(e: dict) -> dict:
    f = e.get("episodeFile") or {}
    q = ((f.get("quality") or {}).get("quality") or {}).get("name")
    return {"app": "sonarr", "item_id": e["id"], "series_id": e.get("seriesId"), "season": e.get("seasonNumber"),
            "episode": e.get("episodeNumber"), "title": e.get("title"), "year": None, "quality": q}


def movie(m: dict) -> dict:
    f = m.get("movieFile") or {}
    q = ((f.get("quality") or {}).get("quality") or {}).get("name")
    return {"id": m["id"], "title": m.get("title"), "year": m.get("year") or None, "tmdb_id": m.get("tmdbId") or None,
            "imdb_id": m.get("imdbId") or None, "monitored": int(bool(m.get("monitored"))),
            "has_file": int(bool(m.get("hasFile"))), "is_available": int(bool(m.get("isAvailable"))),
            "status": m.get("status"), "size_bytes": m.get("sizeOnDisk") or 0, "quality": q,
            "cutoff_not_met": int(bool(f.get("qualityCutoffNotMet"))),
            "collection_tmdb_id": (m.get("collection") or {}).get("tmdbId"),
            "added_at": ts(m.get("added")), "file_added_at": ts(f.get("dateAdded"))}


def collection(c: dict) -> dict:
    films = [{"tmdb_id": x.get("tmdbId"), "title": x.get("title"), "year": x.get("year")} for x in c.get("movies") or []]
    return {"tmdb_id": c["tmdbId"], "title": c.get("title"), "monitored": int(bool(c.get("monitored"))),
            "movies": json.dumps(films)}


def sab_daily(stats: dict) -> dict[str, int]:
    """server_stats -> {YYYY-MM-DD: bytes}, summed over every news server."""
    out: dict[str, int] = {}
    for server in (stats.get("servers") or {}).values():
        for day, b in (server.get("daily") or {}).items():
            out[day] = out.get(day, 0) + int(b or 0)
    return out


def sab_history(slot: dict) -> dict:
    return {"nzo_id": slot.get("nzo_id"), "name": slot.get("name"), "category": slot.get("category"),
            "bytes": int(slot.get("bytes") or 0), "status": slot.get("status"),
            "completed_at": int(slot.get("completed") or 0) or None, "fail_message": slot.get("fail_message") or None}


def sab_queue(q: dict) -> dict:
    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    return {"status": q.get("status"), "paused": bool(q.get("paused")), "items": int(q.get("noofslots") or 0),
            "kbps": f(q.get("kbpersec")), "mb_left": f(q.get("mbleft")), "time_left": q.get("timeleft"),
            "names": [s.get("filename") for s in (q.get("slots") or [])[:5]]}
