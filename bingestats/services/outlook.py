"""Season outlook: is a new season actually coming? (docs/specs/pins.md, ported from Pinnarr)

Sonarr's "continuing" only means TVDB hasn't marked a show ended. Nobody goes
back to update a quietly cancelled show, so it stays "continuing" for years.
The outlook puts Sonarr's dates and seasons together with TMDB's production
status and gives one verdict. It describes the metadata, not inside knowledge:
a show renewed last week reads wrong until someone updates TMDB.

`compute` is a pure function (tests/test_outlook.py pins its order). `for_shows`
gathers the inputs for each show from Sonarr's and TMDB's snapshots, worked out
on query, so it never goes stale in the database.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

DATED, ANNOUNCED, IN_PRODUCTION = "dated", "announced", "in_production"
CANCELLED, ENDED, BETWEEN_SEASONS, DORMANT, UNKNOWN = "cancelled", "ended", "between_seasons", "dormant", "unknown"

# Display order (calendar groups, and later the browser's facet) and labels.
LABELS = {
    DATED: "Dated", ANNOUNCED: "Announced", IN_PRODUCTION: "Filming", BETWEEN_SEASONS: "Hiatus",
    DORMANT: "Probably over", CANCELLED: "Cancelled", ENDED: "Ended", UNKNOWN: "Unknown",
}
# Outlooks that mean there's a future worth waiting for.
HAS_FUTURE = frozenset({DATED, ANNOUNCED, IN_PRODUCTION, BETWEEN_SEASONS})

MONTH_S = 30.44 * 86400
HIATUS_MONTHS, DORMANT_MONTHS = 9, 24   # 24: Marc, 2026-11-05 (18 caught Black Mirror after 19 months)


def compute(*, now: float, next_at: int | None = None, previous_at: int | None = None,
            latest_season: int | None = None, latest_aired_season: int | None = None,
            sonarr_status: str | None = None, tmdb_status: str | None = None,
            in_production: bool | None = None,
            hiatus_months: float = HIATUS_MONTHS, dormant_months: float = DORMANT_MONTHS) -> str:
    """One show's outlook. First match wins, and the order matters:

    - dated, then announced: real scheduling beats any status. A show TMDB calls Ended
      whose finale hasn't aired yet is dated, not ended. A next date in the past is
      stale data and falls through.
    - filming beats a long silence: a show revived after years off is filming, not over.
      Filming means TMDB's *status* "In Production" (or "Post Production"). TMDB's in_production
      flag is no use for this: it's true for every show TMDB hasn't marked finished (checked
      against Marc's library, 2026-11-05: 607 "Returning Series" had it), so it's ignored.
    - cancelled and ended come before hiatus and dormant, so a finished show is never
      described as merely on a break. When TMDB has a status, it decides ended; Sonarr's
      "ended" (from TVDB) counts only when TMDB has nothing, because TVDB marks shows ended
      between series that TMDB knows are coming back.
    - then hiatus (aired within 24 months) or dormant ("probably over").
    """
    tmdb = (tmdb_status or "").strip().lower()
    sonarr = (sonarr_status or "").strip().lower()
    if next_at and next_at > now:
        return DATED
    if latest_season is not None and latest_aired_season is not None and latest_season > latest_aired_season:
        return ANNOUNCED
    if tmdb in ("in production", "post production"):
        return IN_PRODUCTION
    if tmdb == "canceled":
        return CANCELLED
    if tmdb == "ended" or (not tmdb and sonarr == "ended"):
        return ENDED
    if previous_at:
        age = now - previous_at
        return DORMANT if age > dormant_months * MONTH_S else BETWEEN_SEASONS
    if tmdb in ("planned", "pilot") or sonarr == "upcoming":
        return ANNOUNCED        # nothing has aired yet, and something is planned
    return UNKNOWN


def _day(value: str | None) -> int | None:
    """TMDB's '2026-12-01' -> epoch seconds at midnight UTC (it gives dates, not times)."""
    if not value:
        return None
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def _latest(*values):
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def for_shows(conn: sqlite3.Connection, tvdb_ids: list[int] | None = None, now: float | None = None) -> dict[int, dict]:
    """tvdb id -> {outlook, label, next_at}, for shows in Sonarr or with TMDB data.

    - next airing: Sonarr's, else the date TMDB has for the next episode.
    - last airing: the later of Sonarr's and TMDB's.
    - latest season: the highest season Sonarr or TMDB lists (specials aside).
    - latest aired season: TMDB's last episode's season; without TMDB, only from episode lists we
      hold in full (pinned shows), so an unmonitored season in Sonarr can't pass for an announced one.
    """
    now = now or time.time()
    where, params = "", []
    if tvdb_ids is not None:
        if not tvdb_ids:
            return {}
        where, params = f" AND tvdb_id IN ({','.join('?' * len(tvdb_ids))})", list(tvdb_ids)
    sonarr = {r["tvdb_id"]: r for r in conn.execute(
        f"SELECT id, tvdb_id, status, next_airing, previous_airing, seasons FROM sonarr_series "
        f"WHERE tvdb_id IS NOT NULL{where}", params)}
    tmdb = {r["tvdb_id"]: r for r in conn.execute(
        f"SELECT * FROM tmdb_shows WHERE tvdb_id IS NOT NULL{where} ORDER BY fetched_at", params)}
    aired: dict[int, int] = {}
    if sonarr:
        ids = [s["id"] for s in sonarr.values()]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(
                    f"SELECT series_id, MAX(season) FROM sonarr_episodes WHERE season > 0 AND air_at <= ? "
                    f"AND series_id IN ({','.join('?' * len(chunk))}) GROUP BY series_id", [int(now), *chunk]):
                aired[r[0]] = r[1]
        full = {r[0] for r in conn.execute(                     # shows whose whole episode list we hold
            "SELECT DISTINCT s.id FROM sonarr_series s JOIN pins p ON p.tvdb_id = s.tvdb_id")}
    else:
        full = set()

    out: dict[int, dict] = {}
    for tvdb in set(sonarr) | set(tmdb):
        s, t = sonarr.get(tvdb), tmdb.get(tvdb)
        s_seasons = [x.get("season") for x in json.loads(s["seasons"])] if s else []
        s_seasons = [n for n in s_seasons if n and n > 0]
        tmdb_next = _day(t["next_air_date"]) if t else None
        next_at = s["next_airing"] if s and s["next_airing"] and s["next_airing"] > now else \
            (tmdb_next if tmdb_next and tmdb_next > now else None)
        latest_aired = t["last_season"] if t and t["last_season"] is not None else \
            (aired.get(s["id"]) if s and s["id"] in full else None)
        o = compute(now=now, next_at=next_at,
                    previous_at=_latest(s["previous_airing"] if s else None, _day(t["last_air_date"]) if t else None),
                    latest_season=_latest(max(s_seasons) if s_seasons else None, t["max_season"] if t else None),
                    latest_aired_season=latest_aired,
                    sonarr_status=s["status"] if s else None, tmdb_status=t["status"] if t else None,
                    in_production=bool(t["in_production"]) if t and t["in_production"] is not None else None)
        out[tvdb] = {"outlook": o, "label": LABELS[o], "next_at": next_at}
    return out
