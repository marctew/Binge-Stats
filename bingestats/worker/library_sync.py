"""Library and user sync (SPEC.md section 7). Runs as worker jobs."""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable

from .. import db
from ..plex.client import PlexClient, PlexError
from ..plex.parse import (LIBRARY_TYPES, TYPE_NUMBERS, parse_home_users, parse_owner, parse_page,
                          parse_sections, parse_shared_users)
from ..services import jobs, settings
from ..services.libraries import backfill_play_guids

log = logging.getLogger(__name__)

PAGE = 500
Progress = Callable[[int, str], None]

ITEM_COLS = ["rating_key", "type", "guid", "parent_rating_key", "grandparent_rating_key", "title", "parent_title",
             "grandparent_title", "season_index", "episode_index", "year", "duration_s", "added_at",
             "originally_available_at", "thumb", "art", "size_bytes", "video_codec", "video_resolution",
             "video_dynamic_range", "audio_codec", "audio_channels", "container", "bitrate_kbps",
             "tvdb_id", "tmdb_id", "imdb_id"]
# Things a person adds to a library that are worth announcing.
ANNOUNCE = {"movie", "episode", "album"}


def sync_libraries(conn: sqlite3.Connection, client: PlexClient, server_id: int,
                   progress: Progress | None = None, clock: Callable[[], float] = time.time) -> dict:
    progress = progress or (lambda p, m: None)
    started = int(clock())
    sections = [s for s in parse_sections(client.sections()) if s["type"] in LIBRARY_TYPES]
    with db.transaction(conn):
        for s in sections:
            conn.execute(
                "INSERT INTO libraries (id, server_id, name, type, is_active) VALUES (?,?,?,?,1) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, type = excluded.type, is_active = 1",
                (s["id"], server_id, s["name"], s["type"]))
        seen = [s["id"] for s in sections]
        conn.execute(f"UPDATE libraries SET is_active = 0 WHERE id NOT IN ({','.join('?' * len(seen)) or 'NULL'})",
                     seen)

    result = {"libraries": len(sections), "items": 0, "new": 0, "removed": 0}
    for n, s in enumerate(sections):
        first_sync = conn.execute("SELECT synced_at FROM libraries WHERE id = ?", (s["id"],)).fetchone()[0] is None
        for plex_type in LIBRARY_TYPES[s["type"]]:
            start = 0
            while True:
                items, total = parse_page(client.section_items(s["id"], TYPE_NUMBERS[plex_type], start, PAGE))
                new = _upsert_items(conn, items, s["id"], server_id, started, announce=not first_sync)
                result["items"] += len(items)
                result["new"] += new
                start += PAGE
                done_frac = (n + min(1.0, start / max(total, 1))) / max(len(sections), 1)
                progress(int(done_frac * 90), f"{s['name']}: {min(start, total):,} of {total:,} {plex_type}s")
                if not items or start >= total:
                    break
        with db.transaction(conn):
            cur = conn.execute(
                "UPDATE media_items SET is_present = 0 WHERE library_id = ? AND is_present = 1 AND synced_at < ?",
                (s["id"], started))
            result["removed"] += cur.rowcount
            top, child = {"movie": ("movie", None), "show": ("show", "episode"),
                          "artist": ("artist", "track")}[s["type"]]
            conn.execute(
                "UPDATE libraries SET synced_at = ?, "
                "item_count = (SELECT COUNT(*) FROM media_items WHERE library_id = ? AND type = ? AND is_present = 1), "
                "child_count = (SELECT COUNT(*) FROM media_items WHERE library_id = ? AND type = ? AND is_present = 1), "
                "total_size_bytes = (SELECT SUM(size_bytes) FROM media_items WHERE library_id = ? AND is_present = 1) "
                "WHERE id = ?",
                (started, s["id"], top, s["id"], child, s["id"], s["id"]))

    progress(95, "Matching history to library items")
    result["plays_matched"] = backfill_play_guids(conn)
    if result["plays_matched"]:
        jobs.ensure(conn, "rebuild_binges")   # show identities changed
    settings.set(conn, "library.last_sync_at", started)
    return result


def _upsert_items(conn: sqlite3.Connection, items: list[dict], library_id: int, server_id: int,
                  synced_at: int, announce: bool) -> int:
    new = 0
    with db.transaction(conn):
        for it in items:
            if it["rating_key"] is None:
                continue
            existed = conn.execute("SELECT 1 FROM media_items WHERE rating_key = ?", (it["rating_key"],)).fetchone()
            vals = [it[c] for c in ITEM_COLS]
            conn.execute(
                f"INSERT INTO media_items ({', '.join(ITEM_COLS)}, server_id, library_id, is_present, synced_at) "
                f"VALUES ({', '.join('?' * len(ITEM_COLS))}, ?, ?, 1, ?) ON CONFLICT(rating_key) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in ITEM_COLS if c not in ("rating_key", "video_dynamic_range"))
                + ", library_id = excluded.library_id, is_present = 1, synced_at = excluded.synced_at",
                vals + [server_id, library_id, synced_at])
            if not existed:
                new += 1
                if announce and it["type"] in ANNOUNCE:
                    conn.execute("INSERT OR IGNORE INTO recently_added_queue (rating_key, queued_at) VALUES (?, ?)",
                                 (it["rating_key"], synced_at))
    return new


def sync_users(conn: sqlite3.Connection, client: PlexClient, progress: Progress | None = None,
               clock: Callable[[], float] = time.time) -> dict:
    """Owner, home users and shared users from plex.tv. Users missing from all
    three lists are marked inactive, never deleted."""
    progress = progress or (lambda p, m: None)
    now = int(clock())
    progress(10, "Fetching account")
    found: dict[int, dict] = {}
    owner = parse_owner(client.plextv("/api/v2/user"))
    if owner["id"] is not None:
        found[owner["id"]] = owner
    progress(40, "Fetching home users")
    try:
        for u in parse_home_users(client.plextv("/api/v2/home/users")):
            found[u["id"]] = {**found.get(u["id"], {}), **{k: v for k, v in u.items() if v is not None},
                              "is_owner": found.get(u["id"], {}).get("is_owner") or u["is_owner"]}
    except PlexError as e:
        log.info("no Plex Home: %s", e)
    progress(70, "Fetching shared users")
    for u in parse_shared_users(client.plextv("/api/users", accept="application/xml")):
        prev = found.get(u["id"], {})
        found[u["id"]] = {**u, **prev}
    added = 0
    import secrets
    with db.transaction(conn):
        for uid, u in found.items():
            exists = conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone()
            if exists:
                conn.execute(
                    "UPDATE users SET username = COALESCE(?, username), email = COALESCE(?, email), "
                    "thumb_url = COALESCE(?, thumb_url), is_owner = ?, is_home = ?, is_active = 1, "
                    "friendly_name = COALESCE(friendly_name, ?) WHERE id = ?",
                    (u.get("username"), u.get("email"), u.get("thumb_url"), int(bool(u.get("is_owner"))),
                     int(bool(u.get("is_home"))), _friendly(u), uid))
            else:
                conn.execute(
                    "INSERT INTO users (id, username, friendly_name, email, thumb_url, is_owner, is_home, is_active, "
                    "share_salt, first_seen_at) VALUES (?,?,?,?,?,?,?,1,?,?)",
                    (uid, u.get("username"), _friendly(u), u.get("email"), u.get("thumb_url"),
                     int(bool(u.get("is_owner"))), int(bool(u.get("is_home"))), secrets.token_hex(8), now))
                added += 1
        merged = _merge_local_owner(conn, owner["id"]) if owner["id"] not in (None, 1) else 0
        ids = list(found)
        cur = conn.execute(
            f"UPDATE users SET is_active = 0 WHERE is_active = 1 AND id NOT IN ({','.join('?' * len(ids))})", ids)
    settings.set(conn, "users.last_sync_at", now)
    return {"users": len(found), "added": added, "deactivated": cur.rowcount, "owner_plays_merged": merged}


def _merge_local_owner(conn: sqlite3.Connection, owner: int) -> int:
    """Plex sessions call the owner user 1. Anything recorded under 1 before the
    owner's real id was known belongs to the owner."""
    if conn.execute("SELECT 1 FROM users WHERE id = 1").fetchone() is None:
        return 0
    n = conn.execute("UPDATE plays SET user_id = ? WHERE user_id = 1", (owner,)).rowcount
    for table in ("active_sessions", "binges", "alerts"):
        conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id = 1", (owner,))
    conn.execute("UPDATE users SET thumb_url = COALESCE(thumb_url, (SELECT thumb_url FROM users WHERE id = 1)), "
                 "first_seen_at = MIN(COALESCE(first_seen_at, 9e18), COALESCE((SELECT first_seen_at FROM users "
                 "WHERE id = 1), 9e18)) WHERE id = ?", (owner,))
    conn.execute("DELETE FROM users WHERE id = 1")
    return n


def _friendly(u: dict) -> str | None:
    f = u.get("friendly_name")
    return f if f and f != u.get("username") else None
