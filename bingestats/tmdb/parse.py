"""TMDB JSON -> rows for tmdb_shows."""
from __future__ import annotations

import json


def tv(d: dict, tmdb_id: int, tvdb_id: int | None, now: int) -> dict:
    """One show. next_season: the season of the next episode TMDB knows about. max_season: the highest
    season TMDB lists (specials aside), so a season with no dates yet can still be spotted."""
    nxt = d.get("next_episode_to_air") or {}
    last = d.get("last_episode_to_air") or {}
    seasons = [s.get("season_number") for s in d.get("seasons") or [] if (s.get("season_number") or 0) > 0]
    return {"tmdb_id": tmdb_id, "tvdb_id": tvdb_id, "name": d.get("name"), "status": d.get("status"),
            "in_production": int(bool(d.get("in_production"))), "last_air_date": d.get("last_air_date") or None,
            "next_air_date": nxt.get("air_date") or None, "next_season": nxt.get("season_number"),
            "last_season": last.get("season_number"), "max_season": max(seasons) if seasons else None,
            "genres": json.dumps([g.get("name") for g in d.get("genres") or [] if g.get("name")]),
            "networks": json.dumps([n.get("name") for n in d.get("networks") or [] if n.get("name")]),
            "fetched_at": now}


def missing(tmdb_id: int, tvdb_id: int | None, now: int) -> dict:
    """TMDB answered 404: keep a row so the id isn't asked for again until the weekly refresh."""
    return {"tmdb_id": tmdb_id, "tvdb_id": tvdb_id, "name": None, "status": None, "in_production": None,
            "last_air_date": None, "next_air_date": None, "next_season": None, "last_season": None,
            "max_season": None, "genres": "[]", "networks": "[]", "fetched_at": now}
