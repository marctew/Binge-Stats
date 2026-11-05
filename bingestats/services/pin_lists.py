"""The calendar's lists (docs/specs/pins.md, stage 4): Ready, Retire and Discover.

All three start from one picture of a person's viewing per show (`progress`): which of the show's
episodes in Plex they've finished (by guid, so a re-added file still counts), where they've got to
(the furthest episode finished), and when they last played it. Only their own plays count: an
episode marked watched in Plex without playing, or watched on someone else's account, isn't seen.

- Ready: per pinned show, the next episode after where you've got to and how many are waiting.
- Retire: pins that can't give you anything new (finished, probably over) or have gone cold.
  Unpinning a group is one batch, kept in pin_retired so it can be undone.
- Discover: unpinned shows you've watched before that have something coming.
"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict

from . import outlook as outlook_svc
from . import pins

NEW_DAYS = 7               # "new this week" on Ready
COLD_DAYS = 180            # Retire: not played (or, never played, pinned) this long ago
DISCOVER_DAYS = 60         # Discover: dated within this many days
ENDED = {outlook_svc.ENDED, outlook_svc.CANCELLED}


def _show_maps(conn: sqlite3.Connection) -> tuple[dict, dict]:
    guid_to_tvdb, rk_to_tvdb = {}, {}
    for r in conn.execute("SELECT rating_key, guid, tvdb_id FROM media_items WHERE type = 'show' AND tvdb_id IS NOT NULL"):
        rk_to_tvdb[r["rating_key"]] = r["tvdb_id"]
        if r["guid"]:
            guid_to_tvdb[r["guid"]] = r["tvdb_id"]
    return guid_to_tvdb, rk_to_tvdb


def viewing(conn: sqlite3.Connection, user_id: int) -> dict[int, dict]:
    """tvdb id -> {watched: set of episode guids finished, last: last play, plays, seconds}, from this person's plays."""
    guid_to_tvdb, rk_to_tvdb = _show_maps(conn)
    out: dict[int, dict] = defaultdict(lambda: {"watched": set(), "last": 0, "plays": 0, "seconds": 0})
    for r in conn.execute("SELECT grandparent_guid, grandparent_rating_key, guid, watched, started_at, play_s, group_id "
                          "FROM plays WHERE user_id = ? AND media_type = 'episode'", (user_id,)):
        tvdb = guid_to_tvdb.get(r["grandparent_guid"]) or rk_to_tvdb.get(r["grandparent_rating_key"])
        if tvdb is None:
            continue
        v = out[tvdb]
        if r["watched"] and r["guid"]:
            v["watched"].add(r["guid"])
        v["last"] = max(v["last"], r["started_at"] or 0)
        v["seconds"] += r["play_s"] or 0
        if r["group_id"] is None:
            v["plays"] += 1
    return dict(out)


def progress(conn: sqlite3.Connection, user_id: int, tvdb_ids: list[int]) -> dict[int, dict]:
    """For each of these shows in Plex: its episodes in order (specials aside), where this person has
    got to, what's after it, and when they last played it."""
    if not tvdb_ids:
        return {}
    eps: dict[int, list] = defaultdict(list)
    shows = {}
    for r in conn.execute(
            "SELECT s.tvdb_id, s.rating_key AS show_rk, s.title AS show, s.thumb AS show_thumb, e.rating_key, e.guid, "
            "e.title, e.season_index, e.episode_index, e.added_at FROM media_items e "
            "JOIN media_items s ON s.rating_key = e.grandparent_rating_key "
            f"WHERE e.type = 'episode' AND e.is_present = 1 AND s.is_present = 1 AND s.tvdb_id IN ({','.join('?' * len(tvdb_ids))}) "
            "AND e.season_index > 0 AND e.episode_index IS NOT NULL", list(tvdb_ids)):
        eps[r["tvdb_id"]].append(r)
        shows.setdefault(r["tvdb_id"], {"show": r["show"], "rating_key": r["show_rk"], "thumb": r["show_thumb"]})
    seen = viewing(conn, user_id)
    out = {}
    for tvdb, rows in eps.items():
        rows = sorted({(r["season_index"], r["episode_index"]): r for r in rows}.values(),   # one per episode
                      key=lambda r: (r["season_index"], r["episode_index"]))
        v = seen.get(tvdb, {"watched": set(), "last": 0, "plays": 0, "seconds": 0})
        done = [i for i, r in enumerate(rows) if r["guid"] in v["watched"]]
        upto = max(done) if done else -1
        after = rows[upto + 1:]
        out[tvdb] = {**shows[tvdb], "tvdb_id": tvdb, "episodes": rows, "total": len(rows), "finished": len(done),
                     "started": bool(done), "waiting": after, "last": v["last"], "plays": v["plays"]}
    return out


def _code(r) -> str:
    return f"S{int(r['season_index']):02d}E{int(r['episode_index']):02d}"


# -- Ready ---------------------------------------------------------------------------------

def ready(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> list[dict]:
    """One row per pinned show with something in Plex after where you've got to: the next episode and
    how many are waiting. Newest arrival first, so what just landed is at the top."""
    now = now or time.time()
    out = []
    for p in progress(conn, user_id, sorted(pins.pinned(conn, user_id))).values():
        if not p["waiting"]:
            continue
        nxt = p["waiting"][0]
        newest = max(r["added_at"] or 0 for r in p["waiting"])
        out.append({"tvdb_id": p["tvdb_id"], "show": p["show"], "show_rating_key": p["rating_key"], "thumb": p["thumb"],
                    "next_rating_key": nxt["rating_key"], "next_code": _code(nxt), "next_title": nxt["title"],
                    "waiting": len(p["waiting"]), "started": p["started"], "newest_at": newest,
                    "new": newest >= now - NEW_DAYS * 86400, "last": p["last"]})
    out.sort(key=lambda r: (-r["newest_at"], (r["show"] or "").lower()))
    return out


# -- Retire ---------------------------------------------------------------------------------

def retire(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> list[dict]:
    """Pins that can't give you anything new, in groups: finished (ended or cancelled, and you're up to
    date), probably over (nothing for 2 years, up to date), and gone cold (still going, episodes waiting
    in Plex, but not played, or never played and pinned, for COLD_DAYS)."""
    now = now or time.time()
    tvdb_ids = sorted(pins.pinned(conn, user_id))
    if not tvdb_ids:
        return []
    shows = pins._shows(conn, tvdb_ids)
    prog = progress(conn, user_id, tvdb_ids)
    looks = outlook_svc.for_shows(conn, tvdb_ids, now)
    pinned_at = {r[0]: r[1] for r in conn.execute("SELECT tvdb_id, pinned_at FROM pins WHERE user_id = ?", (user_id,))}
    groups = {"finished": [], "over": [], "cold": []}
    for tvdb in tvdb_ids:
        s, p = shows.get(tvdb), prog.get(tvdb)
        if s is None:
            continue
        look = (looks.get(tvdb) or {}).get("outlook", outlook_svc.UNKNOWN)
        up_to_date = p is not None and p["started"] and not p["waiting"]
        row = {"tvdb_id": tvdb, "title": s["title"], "year": s["year"], "rating_key": s["rating_key"], "thumb": s["thumb"],
               "series_id": s["series_id"], "outlook": look, "finished": p["finished"] if p else 0,
               "total": p["total"] if p else 0, "last": p["last"] if p else 0}
        if look in ENDED and up_to_date:
            groups["finished"].append(row)
        elif look == outlook_svc.DORMANT and up_to_date:
            groups["over"].append(row)
        elif look not in ENDED and p is not None and p["waiting"] and \
                (row["last"] or pinned_at.get(tvdb) or now) < now - COLD_DAYS * 86400:
            # stopped part-way: episodes are waiting and you haven't touched it in COLD_DAYS. Being up to
            # date and waiting for the next season is what a pin is for, so that never counts.
            row["waiting"] = len(p["waiting"])
            groups["cold"].append(row)
    labels = {"finished": ("Finished", "Ended or cancelled, and you've watched everything in Plex"),
              "over": ("Probably over", "Nothing for 2 years, and you're up to date"),
              "cold": ("Gone cold", f"Episodes waiting, but not played in {COLD_DAYS // 30} months")}
    return [{"key": k, "label": labels[k][0], "why": labels[k][1],
             "shows": sorted(v, key=lambda r: (r["title"] or "").lower())} for k, v in groups.items() if v]


def retire_group(conn: sqlite3.Connection, user_id: int, group: str, now: float | None = None) -> int:
    """Unpin everything in one Retire group (worked out again here, not taken from the page), as one
    batch that undo_retire can put back. Returns how many were unpinned."""
    now = int(now or time.time())
    g = next((g for g in retire(conn, user_id, now) if g["key"] == group), None)
    if g is None:
        return 0
    batch = f"retire-{now}"
    for s in g["shows"]:
        row = conn.execute("SELECT pinned_at, notify, batch FROM pins WHERE user_id = ? AND tvdb_id = ?",
                           (user_id, s["tvdb_id"])).fetchone()
        if row is None:
            continue
        conn.execute("INSERT OR REPLACE INTO pin_retired (user_id, tvdb_id, pinned_at, notify, pin_batch, batch, retired_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, s["tvdb_id"], row["pinned_at"], row["notify"], row["batch"],
                                                     batch, now))
        pins.unpin(conn, user_id, s["tvdb_id"])
    return len(g["shows"])


def last_retired(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute("SELECT batch, COUNT(*) AS n FROM pin_retired WHERE user_id = ? GROUP BY batch "
                       "ORDER BY MAX(retired_at) DESC LIMIT 1", (user_id,)).fetchone()
    return {"batch": row["batch"], "count": row["n"]} if row else None


def undo_retire(conn: sqlite3.Connection, user_id: int) -> int:
    """Put the last retired batch back, as it was (when it was pinned, notify on or off)."""
    last = last_retired(conn, user_id)
    if last is None:
        return 0
    rows = conn.execute("SELECT * FROM pin_retired WHERE user_id = ? AND batch = ?", (user_id, last["batch"])).fetchall()
    for r in rows:
        conn.execute("INSERT OR IGNORE INTO pins (user_id, tvdb_id, pinned_at, batch, notify) VALUES (?, ?, ?, ?, ?)",
                     (user_id, r["tvdb_id"], r["pinned_at"], r["pin_batch"], r["notify"]))
    conn.execute("DELETE FROM pin_retired WHERE user_id = ? AND batch = ?", (user_id, last["batch"]))
    return len(rows)


# -- Discover --------------------------------------------------------------------------------

def discover(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> list[dict]:
    """Unpinned shows you've watched before with something coming: dated within DISCOVER_DAYS, or a new
    season announced or filming. Most watched first."""
    now = now or time.time()
    seen = {t: v for t, v in viewing(conn, user_id).items() if v["plays"]}
    mine = pins.pinned(conn, user_id)
    candidates = [t for t in seen if t not in mine]
    if not candidates:
        return []
    looks = outlook_svc.for_shows(conn, candidates, now)
    shows = pins._shows(conn, candidates)
    out = []
    for tvdb in candidates:
        look, s = looks.get(tvdb), shows.get(tvdb)
        if not look or not s:
            continue
        o = look["outlook"]
        if o == outlook_svc.DATED and not (look["next_at"] and look["next_at"] <= now + DISCOVER_DAYS * 86400):
            continue
        if o not in (outlook_svc.DATED, outlook_svc.ANNOUNCED, outlook_svc.IN_PRODUCTION):
            continue
        v = seen[tvdb]
        out.append({"tvdb_id": tvdb, "title": s["title"], "year": s["year"], "rating_key": s["rating_key"],
                    "thumb": s["thumb"], "series_id": s["series_id"], "outlook": o, "next_at": look["next_at"],
                    "plays": v["plays"], "hours": round(v["seconds"] / 3600, 1), "last": v["last"]})
    out.sort(key=lambda r: (-r["hours"], -(r["last"] or 0)))
    return out


def counts(conn: sqlite3.Connection, user_id: int, now: float | None = None) -> dict:
    """For the tabs: how many rows each list has."""
    return {"ready": len(ready(conn, user_id, now)), "retire": sum(len(g["shows"]) for g in retire(conn, user_id, now)),
            "discover": len(discover(conn, user_id, now))}
