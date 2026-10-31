"""Libraries, items and users for the UI, plus history-to-library matching."""
from __future__ import annotations

import sqlite3
import time

from . import history


def backfill_play_guids(conn: sqlite3.Connection) -> int:
    """Fill gaps in plays from the library:
    - show guid for episodes (Tautulli doesn't store it), from the show's
      rating key, or failing that from the episode's guid: when a show is
      re-added to Plex its rating keys change but the episode guids don't,
    - a modern plex:// guid where the play has a legacy agent guid but the
      item is still in the library under the same rating key.
    Returns the number of plays changed."""
    n = conn.execute(
        "UPDATE plays SET grandparent_guid = (SELECT mi.guid FROM media_items mi "
        "  WHERE mi.rating_key = plays.grandparent_rating_key) "
        "WHERE grandparent_guid IS NULL AND grandparent_rating_key IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM media_items mi WHERE mi.rating_key = plays.grandparent_rating_key "
        "  AND mi.guid IS NOT NULL)").rowcount
    n += conn.execute(
        "UPDATE plays SET grandparent_guid = (SELECT s.guid FROM media_items e "
        "  JOIN media_items s ON s.rating_key = e.grandparent_rating_key AND s.guid IS NOT NULL "
        "  WHERE e.guid = plays.guid AND e.type = 'episode' LIMIT 1) "
        "WHERE media_type = 'episode' AND grandparent_guid IS NULL AND guid IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM media_items e JOIN media_items s ON s.rating_key = e.grandparent_rating_key "
        "  AND s.guid IS NOT NULL WHERE e.guid = plays.guid AND e.type = 'episode')").rowcount
    n += conn.execute(
        "UPDATE plays SET guid = (SELECT mi.guid FROM media_items mi WHERE mi.rating_key = plays.rating_key) "
        "WHERE (guid IS NULL OR guid NOT LIKE 'plex://%') AND rating_key IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM media_items mi WHERE mi.rating_key = plays.rating_key "
        "  AND mi.type = plays.media_type AND mi.guid LIKE 'plex://%')").rowcount
    return n


# -- users --------------------------------------------------------------------------

def list_users(conn: sqlite3.Connection, include_inactive: bool = True) -> list[dict]:
    month_ago = int(time.time()) - 30 * 86400
    rows = conn.execute(
        """
        SELECT u.*, COALESCE(u.friendly_name, u.username, CAST(u.id AS TEXT)) AS name,
               COUNT(p.id) AS plays, COALESCE(SUM(p.play_s), 0) AS play_s,
               MAX(p.started_at) AS last_played,
               SUM(CASE WHEN p.started_at >= ? THEN 1 ELSE 0 END) AS plays_30d
        FROM users u LEFT JOIN plays p ON p.user_id = u.id
        WHERE (? OR u.is_active = 1)
        GROUP BY u.id
        ORDER BY u.is_active DESC, last_played DESC NULLS LAST, name COLLATE NOCASE
        """, (month_ago, int(include_inactive))).fetchall()
    return [dict(r) for r in rows]


def get_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute(
        "SELECT u.*, COALESCE(u.friendly_name, u.username, CAST(u.id AS TEXT)) AS name FROM users u WHERE id = ?",
        (user_id,)).fetchone()
    if row is None:
        return None
    u = dict(row)
    now = int(time.time())
    stats = conn.execute(
        """SELECT COUNT(*) AS plays, COALESCE(SUM(play_s), 0) AS play_s, MIN(started_at) AS first_play,
                  MAX(started_at) AS last_play,
                  SUM(CASE WHEN started_at >= ? THEN play_s ELSE 0 END) AS play_s_30d,
                  SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END) AS plays_30d,
                  SUM(CASE WHEN decision = 'transcode' THEN 1 ELSE 0 END) AS transcodes,
                  COUNT(DISTINCT player_title) AS devices
           FROM plays WHERE user_id = ? AND group_id IS NULL""",
        (now - 30 * 86400, now - 30 * 86400, user_id)).fetchone()
    u["stats"] = dict(stats)
    u["top_shows"] = [dict(r) for r in conn.execute(
        """SELECT grandparent_title AS title, MAX(thumb) AS thumb, MAX(grandparent_rating_key) AS rating_key,
                  COUNT(*) AS plays, SUM(play_s) AS play_s
           FROM plays WHERE user_id = ? AND media_type = 'episode' AND grandparent_title IS NOT NULL
           GROUP BY COALESCE(grandparent_guid, grandparent_title) ORDER BY play_s DESC LIMIT 6""", (user_id,))]
    u["top_movies"] = [dict(r) for r in conn.execute(
        """SELECT title, MAX(thumb) AS thumb, MAX(rating_key) AS rating_key, year, COUNT(*) AS plays,
                  SUM(play_s) AS play_s
           FROM plays WHERE user_id = ? AND media_type = 'movie'
           GROUP BY COALESCE(guid, title) ORDER BY play_s DESC LIMIT 6""", (user_id,))]
    u["devices"] = [dict(r) for r in conn.execute(
        """SELECT player_title, product, platform, COUNT(*) AS plays, MAX(started_at) AS last_used
           FROM plays WHERE user_id = ? GROUP BY player_title, product ORDER BY last_used DESC LIMIT 8""",
        (user_id,))]
    return u


_KEEP = object()


def update_user(conn: sqlite3.Connection, user_id: int, *, friendly_name: str | None,
                keep_history: bool, max_streams=_KEEP, max_transcodes=_KEEP, limit_action=_KEEP,
                limit_message=_KEEP) -> None:
    """Limits default to unchanged so callers that only rename don't wipe them."""
    name = (friendly_name or "").strip()[:60] or None
    cur = conn.execute("UPDATE users SET friendly_name = ?, keep_history = ? WHERE id = ?",
                       (name, int(keep_history), user_id))
    if not cur.rowcount:
        raise LookupError("No such user")

    def limit(v):
        if v in (None, "", 0, "0"):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError("Limits must be whole numbers") from None
        if not 1 <= n <= 20:
            raise ValueError("Limits must be between 1 and 20, or blank for no limit")
        return n

    if max_streams is not _KEEP:
        conn.execute("UPDATE users SET max_streams = ? WHERE id = ?", (limit(max_streams), user_id))
    if max_transcodes is not _KEEP:
        conn.execute("UPDATE users SET max_transcodes = ? WHERE id = ?", (limit(max_transcodes), user_id))
    if limit_action is not _KEEP:
        if limit_action not in ("notify", "stop"):
            raise ValueError("Over the limit must be 'notify' or 'stop'")
        conn.execute("UPDATE users SET limit_action = ? WHERE id = ?", (limit_action, user_id))
    if limit_message is not _KEEP:
        conn.execute("UPDATE users SET limit_message = ? WHERE id = ?",
                     ((limit_message or "").strip()[:200] or None, user_id))


# -- libraries ----------------------------------------------------------------------

def list_libraries(conn: sqlite3.Connection) -> list[dict]:
    month_ago = int(time.time()) - 30 * 86400
    rows = conn.execute(
        """SELECT l.*,
                  (SELECT COUNT(*) FROM plays p WHERE p.library_id = l.id) AS plays,
                  (SELECT COUNT(*) FROM plays p WHERE p.library_id = l.id AND p.started_at >= ?) AS plays_30d,
                  (SELECT COALESCE(SUM(p.play_s), 0) FROM plays p WHERE p.library_id = l.id) AS play_s,
                  (SELECT MAX(p.started_at) FROM plays p WHERE p.library_id = l.id) AS last_played,
                  (SELECT mi.art FROM media_items mi WHERE mi.library_id = l.id AND mi.art IS NOT NULL
                     AND mi.is_present = 1 AND mi.type IN ('movie','show','artist')
                     ORDER BY mi.added_at DESC LIMIT 1) AS cover_art
           FROM libraries l WHERE l.is_active = 1 ORDER BY l.name COLLATE NOCASE""", (month_ago,)).fetchall()
    return [dict(r) for r in rows]


TOP_TYPE = {"movie": "movie", "show": "show", "artist": "artist"}


def get_library(conn: sqlite3.Connection, library_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()
    if row is None:
        return None
    lib = dict(row)
    top = TOP_TYPE.get(lib["type"], "movie")
    lib["recently_added"] = [dict(r) for r in conn.execute(
        "SELECT * FROM media_items WHERE library_id = ? AND is_present = 1 AND type = ? "
        "ORDER BY added_at DESC LIMIT 12", (library_id, top if top != "artist" else "album"))]
    if lib["type"] == "show":
        lib["most_played"] = [dict(r) for r in conn.execute(
            """SELECT grandparent_title AS title, MAX(grandparent_rating_key) AS rating_key, MAX(thumb) AS thumb,
                      COUNT(*) AS plays, COUNT(DISTINCT user_id) AS users, SUM(play_s) AS play_s
               FROM plays WHERE library_id = ? AND grandparent_title IS NOT NULL
               GROUP BY COALESCE(grandparent_guid, grandparent_title) ORDER BY plays DESC LIMIT 10""",
            (library_id,))]
    else:
        lib["most_played"] = [dict(r) for r in conn.execute(
            """SELECT COALESCE(grandparent_title || ' · ', '') || title AS title, MAX(rating_key) AS rating_key,
                      MAX(thumb) AS thumb, COUNT(*) AS plays, COUNT(DISTINCT user_id) AS users, SUM(play_s) AS play_s
               FROM plays WHERE library_id = ? GROUP BY COALESCE(guid, title) ORDER BY plays DESC LIMIT 10""",
            (library_id,))]
    lib["stats"] = dict(conn.execute(
        "SELECT COUNT(*) AS plays, COALESCE(SUM(play_s), 0) AS play_s, COUNT(DISTINCT user_id) AS users "
        "FROM plays WHERE library_id = ?", (library_id,)).fetchone())
    return lib


# -- items --------------------------------------------------------------------------

def get_item(conn: sqlite3.Connection, rating_key: int) -> dict | None:
    """A movie, show, season, episode, album or track: from the library if it's
    still there, else rebuilt from the history snapshot."""
    row = conn.execute("SELECT * FROM media_items WHERE rating_key = ?", (rating_key,)).fetchone()
    item = dict(row) if row else None
    if item is None:
        p = conn.execute(
            "SELECT * FROM plays WHERE rating_key = ? OR grandparent_rating_key = ? OR parent_rating_key = ? "
            "ORDER BY started_at DESC LIMIT 1", (rating_key, rating_key, rating_key)).fetchone()
        if p is None:
            return None
        p = dict(p)
        if p["rating_key"] == rating_key:
            item = {k: p.get(k) for k in ("rating_key", "guid", "title", "parent_title", "grandparent_title",
                                          "season_index", "episode_index", "year", "thumb", "duration_s",
                                          "grandparent_rating_key", "parent_rating_key")}
            item["type"] = p["media_type"]
        elif p["grandparent_rating_key"] == rating_key:
            item = {"rating_key": rating_key, "type": "show" if p["media_type"] == "episode" else "artist",
                    "title": p["grandparent_title"], "guid": p["grandparent_guid"], "thumb": p["thumb"]}
        else:
            item = {"rating_key": rating_key, "type": "season" if p["media_type"] == "episode" else "album",
                    "title": p["parent_title"], "grandparent_title": p["grandparent_title"],
                    "grandparent_rating_key": p["grandparent_rating_key"], "thumb": p["thumb"]}
        item["is_present"] = 0
        item["from_history"] = True

    t = item["type"]
    if t in ("show", "artist"):
        where, params = "(p.grandparent_rating_key = ?" + (" OR p.grandparent_guid = ?)" if item.get("guid") else ")"), \
            [rating_key] + ([item["guid"]] if item.get("guid") else [])
        if t == "show":
            item["episodes"] = conn.execute(
                "SELECT COUNT(*) FROM media_items WHERE grandparent_rating_key = ? AND type = 'episode' "
                "AND is_present = 1", (rating_key,)).fetchone()[0]
    elif t in ("season", "album"):
        where, params = "p.parent_rating_key = ?", [rating_key]
    else:
        where, params = "(p.rating_key = ?" + (" OR p.guid = ?)" if item.get("guid") else ")"), \
            [rating_key] + ([item["guid"]] if item.get("guid") else [])

    item["stats"] = dict(conn.execute(
        f"SELECT COUNT(*) AS plays, COALESCE(SUM(p.play_s), 0) AS play_s, COUNT(DISTINCT p.user_id) AS users, "
        f"MAX(p.started_at) AS last_played FROM plays p WHERE {where}", params).fetchone())
    item["watchers"] = [dict(r) for r in conn.execute(
        f"""SELECT p.user_id, COALESCE(u.friendly_name, u.username, CAST(p.user_id AS TEXT)) AS name,
                   COUNT(*) AS plays, SUM(p.play_s) AS play_s, MAX(p.started_at) AS last_played,
                   MAX(p.watched) AS watched,
                   COUNT(DISTINCT CASE WHEN p.watched = 1 THEN COALESCE(p.guid, CAST(p.rating_key AS TEXT)) END)
                     AS watched_items
            FROM plays p LEFT JOIN users u ON u.id = p.user_id WHERE {where}
            GROUP BY p.user_id ORDER BY last_played DESC""", params)]
    item["history"] = [dict(r) for r in conn.execute(
        f"""SELECT p.*, COALESCE(u.friendly_name, u.username, CAST(p.user_id AS TEXT)) AS user_name
            FROM plays p LEFT JOIN users u ON u.id = p.user_id WHERE {where}
            ORDER BY p.started_at DESC LIMIT 50""", params)]
    if item.get("grandparent_rating_key") and not item.get("from_history"):
        show = conn.execute("SELECT art, thumb FROM media_items WHERE rating_key = ?",
                            (item["grandparent_rating_key"],)).fetchone()
        if show:
            item.setdefault("art", None)
            item["art"] = item["art"] or show["art"]
            item["show_thumb"] = show["thumb"]
    return item


def user_history(conn: sqlite3.Connection, user_id: int, tz: str, limit: int = 15):
    f = history.HistoryFilter(user_id=user_id)
    return history.list_history(conn, f, tz, limit=limit)
