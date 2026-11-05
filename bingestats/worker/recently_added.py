"""Spot new library items quickly and turn them into digest events.

- `check_recent` asks each library for its newest items (one small request
  per library and type) and queues anything not seen before. The full sync
  catches everything else every few hours.
- `digest` waits until nothing new has arrived for `notify.digest_wait_min`,
  then groups the queue: one event per show+season, one per batch of films
  in a library, one per artist's albums.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict

from ..plex.client import PlexClient, PlexError
from ..plex.parse import TYPE_NUMBERS, parse_page
from ..services import settings
from .library_sync import _upsert_items

log = logging.getLogger(__name__)

RECENT_TYPES = {"movie": ["movie"], "show": ["show", "episode"], "artist": ["album"]}


def check_recent(conn: sqlite3.Connection, client: PlexClient, server_id: int, now: float | None = None) -> int:
    """Returns the number of new items queued."""
    now = int(now or time.time())
    queued = 0
    libs = conn.execute("SELECT id, type, synced_at FROM libraries WHERE is_active = 1 AND synced_at IS NOT NULL").fetchall()
    for lib in libs:
        for plex_type in RECENT_TYPES.get(lib["type"], []):
            try:
                items, _total = parse_page(client.section_recent(lib["id"], TYPE_NUMBERS[plex_type]))
            except PlexError as e:
                log.debug("recent check for library %s failed: %s", lib["id"], e)
                continue
            queued += _upsert_items(conn, items, lib["id"], server_id, now, announce=True)
    settings.set(conn, "library.last_recent_check_at", now)
    return queued


def _ep_label(row) -> str:
    se = (f"S{int(row['season_index']):02d}E{int(row['episode_index']):02d}"
          if row["season_index"] is not None and row["episode_index"] is not None else "")
    return " · ".join(x for x in (se, row["title"]) if x)


def digest(conn: sqlite3.Connection, now: float | None = None) -> list[dict]:
    """Group queued additions into events once the queue has gone quiet."""
    now = int(now or time.time())
    newest = conn.execute("SELECT MAX(queued_at) FROM recently_added_queue WHERE processed_at IS NULL").fetchone()[0]
    if newest is None:
        return []
    wait = int(settings.get(conn, "notify.digest_wait_min")) * 60
    if newest > now - wait:
        return []          # still arriving: a season drop lands over several minutes
    rows = conn.execute(
        """SELECT q.rating_key, mi.* FROM recently_added_queue q JOIN media_items mi ON mi.rating_key = q.rating_key
           WHERE q.processed_at IS NULL ORDER BY mi.added_at, mi.season_index, mi.episode_index, mi.title""").fetchall()
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if r["type"] == "episode":
            groups[("episode", r["grandparent_rating_key"], r["season_index"])].append(r)
        elif r["type"] == "movie":
            groups[("movie", r["library_id"])].append(r)
        elif r["type"] == "album":
            groups[("album", r["library_id"], r["parent_title"])].append(r)

    events = []
    for key, items in groups.items():
        first = items[0]
        kind = key[0]
        if kind == "episode":
            show = first["grandparent_title"] or "A show"
            season = first["season_index"]
            show_row = conn.execute("SELECT thumb FROM media_items WHERE rating_key = ?",
                                    (first["grandparent_rating_key"],)).fetchone()
            if len(items) == 1:
                headline = f"{show} · {_ep_label(first)}"
            else:
                headline = f"{show} · Season {season} · {len(items)} new episodes" if season is not None \
                    else f"{show} · {len(items)} new episodes"
            event = {"kind": "episode", "show": show, "season": season, "show_rating_key": first["grandparent_rating_key"],
                     "poster": (show_row["thumb"] if show_row and show_row["thumb"] else first["thumb"]),
                     "items": [{"title": r["title"], "season": r["season_index"], "episode": r["episode_index"],
                                "year": r["year"], "display_title": f"{show} · {_ep_label(r)}"} for r in items]}
        elif kind == "movie":
            headline = (f"{first['title']} ({first['year']})" if first["year"] else first["title"]) if len(items) == 1 \
                else f"{len(items)} new films"
            event = {"kind": "movie", "show": None, "season": None, "poster": first["thumb"],
                     "items": [{"title": r["title"], "year": r["year"],
                                "display_title": f"{r['title']} ({r['year']})" if r["year"] else r["title"]} for r in items]}
        else:
            artist = first["parent_title"] or "Unknown artist"
            headline = f"{artist} · {first['title']}" if len(items) == 1 else f"{artist} · {len(items)} new albums"
            event = {"kind": "album", "show": artist, "season": None, "poster": first["thumb"],
                     "items": [{"title": r["title"], "year": r["year"],
                                "display_title": f"{artist} · {r['title']}"} for r in items]}
        event.update(library_id=first["library_id"], headline=headline,
                     key=":".join(str(k) for k in key) + f":{min(r['rating_key'] for r in items)}")
        events.append(event)

    keys = [r["rating_key"] for r in rows]
    # Anything queued that has since vanished from media_items is dropped too.
    conn.execute("UPDATE recently_added_queue SET processed_at = ? WHERE processed_at IS NULL AND queued_at <= ?",
                 (now, newest))
    log.info("recently added digest: %d group(s) from %d item(s)", len(events), len(keys))
    return events
