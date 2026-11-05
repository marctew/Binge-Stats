"""The shows browser (/shows, /me/shows): every show you could pin, with filters (docs/specs/pins.md, P2).

The default view has no filters, and a big library has 2,000+ shows, so it pages
(60 at a time). Filters AND together; ticking more values within one filter ORs
them. Each filter's counts are worked out with that filter left out, so a count
answers "what would I get if I ticked this as well" (counting with the filter
applied to itself would make every unticked value read zero).

"Pin all shown" re-runs the filter on the server from the query string rather
than taking a list of ids, so it can't act on a stale page. Each bulk pin gets a
batch id, so the last one can be undone.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from . import outlook as outlook_svc
from . import pins

PAGE = 60
FACETS = ("library", "outlook", "status", "genre", "network", "pinned")
FACET_LABELS = {"library": "Library", "outlook": "Outlook", "status": "Status in Sonarr", "genre": "Genre",
                "network": "Network", "pinned": "Pinned"}
SORTS = {"recent": "Recently watched", "title": "Title", "next": "Next airing", "outlook": "Outlook"}
NOT_IN_PLEX, NOT_IN_SONARR = "Not in Plex", "Not in Sonarr"
OUTLOOK_ORDER = list(outlook_svc.LABELS)


@dataclass
class Filter:
    q: str = ""
    picked: dict[str, set[str]] = field(default_factory=lambda: {f: set() for f in FACETS})
    sort: str = "recent"
    page: int = 1

    @classmethod
    def from_args(cls, args) -> Filter:
        f = cls(q=(args.get("q") or "").strip()[:100])
        for name in FACETS:   # ticked boxes send name=a&name=b; links send name=a,b
            raw = args.getlist(name) if hasattr(args, "getlist") else [args.get(name) or ""]
            f.picked[name] = {v for part in raw for v in (part or "").split(",") if v}
        f.sort = args.get("sort") if args.get("sort") in SORTS else "recent"
        try:
            f.page = max(1, int(args.get("page") or 1))
        except ValueError:
            f.page = 1
        return f

    def query(self, **change) -> dict:
        """The query string for this view, with changes (for links and paging)."""
        out = {"q": self.q, **{n: ",".join(sorted(v)) for n, v in self.picked.items()}, "sort": self.sort,
               "page": self.page}
        out.update(change)
        return {k: v for k, v in out.items() if v not in ("", None, 1) and not (k == "sort" and v == "recent")}

    def toggled(self, facet: str, value: str) -> dict:
        vals = set(self.picked[facet]) ^ {value}
        return self.query(**{facet: ",".join(sorted(vals)), "page": 1})

    @property
    def active(self) -> bool:
        return bool(self.q or any(self.picked.values()))


def catalogue(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> list[dict]:
    """Every show with a TVDB id (in Plex or Sonarr), with what the filters and sorts need."""
    now = now or time.time()
    shows = pins._shows(conn)
    libs = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM libraries")}
    lib_of: dict[int, int] = {}
    guid_to_tvdb: dict[str, int] = {}
    rk_to_tvdb: dict[int, int] = {}
    for r in conn.execute("SELECT rating_key, guid, tvdb_id, library_id FROM media_items WHERE type = 'show' "
                          "AND tvdb_id IS NOT NULL"):
        rk_to_tvdb[r["rating_key"]] = r["tvdb_id"]
        if r["guid"]:
            guid_to_tvdb[r["guid"]] = r["tvdb_id"]
        lib_of.setdefault(r["tvdb_id"], r["library_id"])
    sonarr = {r["tvdb_id"]: r for r in conn.execute(
        "SELECT tvdb_id, status, network, genres FROM sonarr_series WHERE tvdb_id IS NOT NULL")}
    tmdb = {r["tvdb_id"]: r for r in conn.execute(
        "SELECT tvdb_id, genres, networks FROM tmdb_shows WHERE tvdb_id IS NOT NULL ORDER BY fetched_at")}
    looks = outlook_svc.for_shows(conn, now=now)
    mine = pins.pinned(conn, user_id)
    watched: dict[int, int] = {}
    for g, rk, last in conn.execute(
            "SELECT grandparent_guid, grandparent_rating_key, MAX(started_at) FROM plays WHERE user_id = ? "
            "AND media_type = 'episode' GROUP BY grandparent_guid, grandparent_rating_key", (user_id,)):
        tvdb = guid_to_tvdb.get(g) or rk_to_tvdb.get(rk)
        if tvdb is not None and last and last > watched.get(tvdb, 0):
            watched[tvdb] = last

    out = []
    for tvdb, s in shows.items():
        so, tm = sonarr.get(tvdb), tmdb.get(tvdb)
        genres = set(json.loads(so["genres"]) if so else []) | set(json.loads(tm["genres"]) if tm else [])
        network = (so["network"] if so else None) or ((json.loads(tm["networks"]) or [None])[0] if tm else None)
        look = looks.get(tvdb) or {"outlook": outlook_svc.UNKNOWN, "next_at": None}
        lib_id = lib_of.get(tvdb) if s.get("rating_key") else None
        out.append({**s, "outlook": look["outlook"], "next_at": look["next_at"],
                    "pinned": tvdb in mine, "last_watched": watched.get(tvdb),
                    "facets": {"library": {libs.get(lib_id, "Plex") if lib_id else NOT_IN_PLEX},
                               "outlook": {look["outlook"]},
                               "status": {(so["status"] or "").capitalize() or NOT_IN_SONARR} if so else {NOT_IN_SONARR},
                               "genre": genres, "network": {network} if network else set(),
                               "pinned": {"yes" if tvdb in mine else "no"}}})
    return out


def _matches(s: dict, f: Filter, skip: str | None = None) -> bool:
    if f.q and f.q.lower() not in (s.get("title") or "").lower():
        return False
    for name, wanted in f.picked.items():
        if name != skip and wanted and not (s["facets"][name] & wanted):
            return False
    return True


def _sort(rows: list[dict], how: str) -> list[dict]:
    title = lambda s: (s.get("title") or "").lower()  # noqa: E731
    if how == "title":
        return sorted(rows, key=title)
    if how == "next":
        return sorted(rows, key=lambda s: (s["next_at"] is None, s["next_at"] or 0, title(s)))
    if how == "outlook":
        return sorted(rows, key=lambda s: (OUTLOOK_ORDER.index(s["outlook"]), s["next_at"] or 0, title(s)))
    return sorted(rows, key=lambda s: (s["last_watched"] is None, -(s["last_watched"] or 0), title(s)))


def _label(facet: str, value: str) -> str:
    if facet == "outlook":
        return outlook_svc.LABELS.get(value, value)
    if facet == "pinned":
        return "Pinned" if value == "yes" else "Not pinned"
    return value


def matching(conn: sqlite3.Connection, user_id: int, f: Filter, now: float | None = None) -> list[dict]:
    return _sort([s for s in catalogue(conn, user_id, now) if _matches(s, f)], f.sort)


def browse(conn: sqlite3.Connection, user_id: int, f: Filter, now: float | None = None) -> dict:
    """One page of shows, the filter counts, and the paging."""
    allshows = catalogue(conn, user_id, now)
    rows = _sort([s for s in allshows if _matches(s, f)], f.sort)
    facets = []
    for name in FACETS:
        counts: dict[str, int] = {}
        for s in allshows:
            if _matches(s, f, skip=name):
                for v in s["facets"][name]:
                    counts[v] = counts.get(v, 0) + 1
        for v in f.picked[name]:                 # a ticked value stays on the list even at zero
            counts.setdefault(v, 0)
        if name == "outlook":
            order = sorted(counts, key=lambda v: OUTLOOK_ORDER.index(v) if v in OUTLOOK_ORDER else 99)
        elif name == "pinned":
            order = [v for v in ("yes", "no") if v in counts]
        else:
            order = sorted(counts, key=lambda v: (v in (NOT_IN_PLEX, NOT_IN_SONARR), -counts[v], v.lower()))
        facets.append({"name": name, "label": FACET_LABELS[name],
                       "options": [{"value": v, "label": _label(name, v), "count": counts[v],
                                   "checked": v in f.picked[name]} for v in order]})
    pages = max(1, -(-len(rows) // PAGE))
    page = min(f.page, pages)
    return {"items": rows[(page - 1) * PAGE: page * PAGE], "total": len(rows), "all": len(allshows),
            "pinned": sum(1 for s in allshows if s["pinned"]), "facets": facets, "page": page, "pages": pages,
            "unpinned_shown": sum(1 for s in rows if not s["pinned"]),
            "last_batch": last_batch(conn, user_id)}


# -- bulk pin ------------------------------------------------------------------------------

def pin_all(conn: sqlite3.Connection, user_id: int, f: Filter, now: float | None = None) -> int:
    """Pin every show the filter matches (all pages, re-run here). Returns how many were newly pinned."""
    now = int(now or time.time())
    batch = f"bulk-{now}"
    n = 0
    for s in matching(conn, user_id, f, now):
        if not s["pinned"]:
            conn.execute("INSERT OR IGNORE INTO pins (user_id, tvdb_id, pinned_at, batch) VALUES (?, ?, ?, ?)",
                         (user_id, s["tvdb_id"], now, batch))
            n += 1
    if n:
        from . import arr, jobs
        if arr.configured(conn)["sonarr"]:
            jobs.ensure(conn, "pin_guides")
    return n


def last_batch(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """The most recent bulk pin that still has pins in it: {batch, count}."""
    row = conn.execute("SELECT batch, COUNT(*) AS n FROM pins WHERE user_id = ? AND batch IS NOT NULL "
                       "GROUP BY batch ORDER BY MAX(pinned_at) DESC LIMIT 1", (user_id,)).fetchone()
    return {"batch": row["batch"], "count": row["n"]} if row else None


def undo_last(conn: sqlite3.Connection, user_id: int) -> int:
    lb = last_batch(conn, user_id)
    if lb is None:
        return 0
    return conn.execute("DELETE FROM pins WHERE user_id = ? AND batch = ?", (user_id, lb["batch"])).rowcount
