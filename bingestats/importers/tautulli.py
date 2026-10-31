"""Import history from a Tautulli database (SPEC.md section 8).

Reads a *copy* of tautulli.db made with the SQLite backup API, so a live
Tautulli with WAL files is safe to read. The import is idempotent: every play
carries source='tautulli' and source_id=session_history.id, so running it
twice adds nothing the second time.

Units checked against Tautulli v2.18.2 (docs/verification.md): durations and
view offsets are milliseconds, started/stopped/paused_counter are seconds.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Callable

from .. import db
from ..services import settings
from ..transcode import infer_reasons, normalise_decision, normalise_resolution, overall_decision

BATCH = 2000
OVERLAP_S = 60

REQUIRED = {
    "session_history": {"id", "reference_id", "started", "stopped", "rating_key", "user_id", "user",
                        "ip_address", "paused_counter", "player", "product", "platform", "machine_id",
                        "bandwidth", "location", "secure", "relayed", "parent_rating_key",
                        "grandparent_rating_key", "media_type", "section_id", "view_offset"},
    "session_history_metadata": {"id", "title", "parent_title", "grandparent_title", "media_index",
                                 "parent_media_index", "thumb", "parent_thumb", "grandparent_thumb",
                                 "year", "duration", "guid"},
    "session_history_media_info": {"id", "video_decision", "audio_decision", "transcode_decision",
                                   "duration", "container", "bitrate", "video_codec", "video_resolution",
                                   "audio_codec", "audio_channels", "stream_container", "stream_bitrate",
                                   "stream_video_codec", "stream_video_resolution", "stream_audio_codec",
                                   "stream_audio_channels", "stream_subtitle_decision"},
    "users": {"user_id", "username", "friendly_name", "thumb", "email", "is_admin", "keep_history",
              "deleted_user"},
}
# Columns used when present, NULL otherwise (older Tautulli versions).
OPTIONAL = {
    "session_history": {"quality_profile"},
    "session_history_media_info": {"video_dynamic_range", "stream_video_dynamic_range", "subtitle_codec",
                                   "stream_subtitle_codec", "transcode_hw_decoding", "transcode_hw_encoding",
                                   "transcode_hw_full_pipeline", "stream_video_decision", "stream_audio_decision"},
    "session_history_metadata": {"art"},
    "users": {"is_home_user"},
}

Progress = Callable[[int, str], None]


class TautulliImportError(Exception):
    """Readable reason the import can't go ahead."""


def snapshot(path: str | os.PathLike, work_dir: str | os.PathLike) -> Path:
    """Consistent copy of the Tautulli database via the backup API."""
    path = Path(path)
    if not path.is_file():
        raise TautulliImportError(f"No file at {path}. Mount Tautulli's data folder and check the path.")
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="tautulli-", suffix=".db", dir=work_dir)
    os.close(fd)
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            src.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        except sqlite3.OperationalError:
            # Read-only mount of a WAL database: mode=ro can't create the -shm
            # file. immutable=1 reads the main file only, which is fine for a
            # copy taken while Tautulli is stopped or after a checkpoint.
            src.close()
            src = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
        with closing(src), closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
            # The copy inherits WAL mode; switch it off so reading the copy
            # doesn't leave -wal/-shm files behind.
            dst.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.DatabaseError as e:
        remove_copy(Path(tmp))
        raise TautulliImportError(f"Couldn't read {path.name}: {e}") from e
    return Path(tmp)


def remove_copy(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _columns(src: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in src.execute(f"PRAGMA table_info({table})")}


def check_schema(src: sqlite3.Connection) -> dict[str, set[str]]:
    """Returns the optional columns present per table. Raises on anything missing."""
    missing = []
    present_optional: dict[str, set[str]] = {}
    for table, cols in REQUIRED.items():
        have = _columns(src, table)
        if not have:
            missing.append(f"table {table}")
            continue
        for c in sorted(cols - have):
            missing.append(f"{table}.{c}")
        present_optional[table] = OPTIONAL.get(table, set()) & have
    if missing:
        raise TautulliImportError("This doesn't look like a Tautulli database Binge Stats understands. Missing: "
                           + ", ".join(missing[:12]) + ("…" if len(missing) > 12 else ""))
    return present_optional


def _open(path: Path) -> sqlite3.Connection:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    return src


def inspect(conn: sqlite3.Connection, path: str | os.PathLike, work_dir: str | os.PathLike) -> dict:
    """Dry run: what an import would do. Writes nothing."""
    copy = snapshot(path, work_dir)
    try:
        with closing(_open(copy)) as src:
            check_schema(src)
            total, first, last = src.execute(
                "SELECT COUNT(*), MIN(started), MAX(started) FROM session_history").fetchone()
            by_type = {r[0] or "unknown": r[1] for r in src.execute(
                "SELECT media_type, COUNT(*) FROM session_history GROUP BY 1 ORDER BY 2 DESC")}
            users = [dict(r) for r in src.execute(
                "SELECT u.user_id, COALESCE(NULLIF(u.friendly_name, ''), u.username) AS name, "
                "COUNT(h.id) AS plays, MAX(h.started) AS last_play FROM users u "
                "LEFT JOIN session_history h ON h.user_id = u.user_id WHERE u.user_id IS NOT NULL "
                "GROUP BY u.user_id ORDER BY plays DESC")]
            missing_users = src.execute(
                "SELECT COUNT(*) FROM session_history WHERE user_id IS NULL OR user_id NOT IN "
                "(SELECT user_id FROM users WHERE user_id IS NOT NULL)").fetchone()[0]
            ids = [r[0] for r in src.execute("SELECT id FROM session_history")]
        already = _count_existing(conn, ids)
        return {
            "plays": total, "first_play": first, "last_play": last, "by_type": by_type,
            "users": users, "missing_users": missing_users, "already_imported": already,
            "to_import": total - already, "plex_connected": _server_id(conn) is not None,
        }
    finally:
        remove_copy(copy)


def _count_existing(conn: sqlite3.Connection, ids: list[int]) -> int:
    n = 0
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        n += conn.execute(
            f"SELECT COUNT(*) FROM plays WHERE source = 'tautulli' AND source_id IN ({','.join('?' * len(chunk))})",
            chunk).fetchone()[0]
    return n


def _server_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM servers ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def run(conn: sqlite3.Connection, path: str | os.PathLike, work_dir: str | os.PathLike,
        progress: Progress | None = None) -> dict:
    progress = progress or (lambda pct, msg: None)
    server_id = _server_id(conn)
    if server_id is None:
        raise TautulliImportError("Connect Plex first, so imported plays can be tied to your server.")
    progress(1, "Copying the Tautulli database")
    copy = snapshot(path, work_dir)
    try:
        with closing(_open(copy)) as src:
            optional = check_schema(src)
            return _import(conn, src, optional, server_id, progress)
    finally:
        remove_copy(copy)


def _sel(alias: str, table: str, cols: set[str], optional: dict[str, set[str]]) -> str:
    out = []
    for c in sorted(cols):
        out.append(f"{alias}.{c} AS {alias}_{c}")
    for c in sorted(OPTIONAL.get(table, set())):
        out.append(f"{alias}.{c} AS {alias}_{c}" if c in optional.get(table, set()) else f"NULL AS {alias}_{c}")
    return ", ".join(out)


def _import(conn: sqlite3.Connection, src: sqlite3.Connection, optional: dict[str, set[str]],
            server_id: int, progress: Progress) -> dict:
    result = {"imported": 0, "skipped_existing": 0, "skipped_overlap": 0, "skipped_no_user": 0,
              "skipped_keep_history": 0, "failed": 0, "errors": [], "users_added": 0, "groups_linked": 0}

    progress(3, "Importing users")
    result["users_added"] = _import_users(conn, src, optional)
    known_users = {r[0]: r[1] for r in conn.execute("SELECT id, keep_history FROM users")}

    cfg = settings.get_many(conn, "ingest.")
    total = src.execute("SELECT COUNT(*) FROM session_history").fetchone()[0] or 1
    query = (
        "SELECT " + ", ".join([
            _sel("h", "session_history", REQUIRED["session_history"], optional),
            _sel("m", "session_history_metadata", REQUIRED["session_history_metadata"] - {"id"}, optional),
            _sel("i", "session_history_media_info", REQUIRED["session_history_media_info"] - {"id"}, optional),
        ]) +
        " FROM session_history h"
        " LEFT JOIN session_history_metadata m ON m.id = h.id"
        " LEFT JOIN session_history_media_info i ON i.id = h.id"
        " WHERE h.id > ? ORDER BY h.id LIMIT ?"
    )
    links: list[tuple[int, int]] = []   # (source_id, reference_id)
    done = 0
    last_id = -1
    while True:
        rows = src.execute(query, (last_id, BATCH)).fetchall()
        if not rows:
            break
        last_id = rows[-1]["h_id"]
        with db.transaction(conn):
            for r in rows:
                done += 1
                uid = r["h_user_id"]
                if uid is None or uid not in known_users:
                    result["skipped_no_user"] += 1
                    continue
                if not known_users[uid]:
                    result["skipped_keep_history"] += 1
                    continue
                try:
                    values = _map_play(r, cfg, server_id)
                except Exception as e:  # noqa: BLE001 - one bad row mustn't sink the import
                    result["failed"] += 1
                    if len(result["errors"]) < 20:
                        result["errors"].append(f"row {r['h_id']}: {e.__class__.__name__}: {e}")
                    continue
                if _overlaps_live(conn, values):
                    result["skipped_overlap"] += 1
                    continue
                cols = ", ".join(values)
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO plays ({cols}) VALUES ({', '.join('?' * len(values))})",
                    list(values.values()))
                if cur.rowcount:
                    result["imported"] += 1
                    ref = r["h_reference_id"]
                    if ref and ref != r["h_id"]:
                        links.append((r["h_id"], ref))
                else:
                    result["skipped_existing"] += 1
        progress(5 + int(done / total * 85), f"Imported {result['imported']:,} of {total:,} plays")

    progress(92, "Linking resumed plays")
    with db.transaction(conn):
        for source_id, ref in links:
            cur = conn.execute(
                "UPDATE plays SET group_id = (SELECT p2.id FROM plays p2 WHERE p2.source = 'tautulli' "
                "AND p2.source_id = ?) WHERE source = 'tautulli' AND source_id = ? AND group_id IS NULL",
                (ref, source_id))
            result["groups_linked"] += cur.rowcount
        # A head that was itself skipped leaves NULL, which is right: those
        # parts then stand alone. Never point a head at itself.
        conn.execute("UPDATE plays SET group_id = NULL WHERE group_id = id")
        conn.execute("DELETE FROM wrapped_cache")
        # first seen should reflect history, not when the user row was made
        conn.execute("UPDATE users SET first_seen_at = (SELECT MIN(started_at) FROM plays WHERE user_id = users.id) "
                     "WHERE EXISTS (SELECT 1 FROM plays WHERE user_id = users.id)")

    progress(96, "Matching to library items")
    from ..services.libraries import backfill_play_guids
    result["guids_backfilled"] = backfill_play_guids(conn)
    result["missing_show_guid"] = conn.execute(
        "SELECT COUNT(*) FROM plays WHERE media_type = 'episode' AND grandparent_guid IS NULL").fetchone()[0]
    return result


def _import_users(conn: sqlite3.Connection, src: sqlite3.Connection, optional: dict[str, set[str]]) -> int:
    has_home = "is_home_user" in optional.get("users", set())
    added = 0
    now = int(time.time())
    with db.transaction(conn):
        for u in src.execute("SELECT * FROM users WHERE user_id IS NOT NULL"):
            exists = conn.execute("SELECT 1 FROM users WHERE id = ?", (u["user_id"],)).fetchone()
            friendly = (u["friendly_name"] or "").strip() or None
            if friendly == u["username"]:
                friendly = None
            if exists:
                # Fill gaps only; never overwrite what Binge Stats already knows.
                conn.execute(
                    "UPDATE users SET username = COALESCE(username, ?), friendly_name = COALESCE(friendly_name, ?), "
                    "email = COALESCE(email, ?), thumb_url = COALESCE(thumb_url, ?) WHERE id = ?",
                    (u["username"], friendly, u["email"] or None, _thumb(u["thumb"]), u["user_id"]))
            else:
                conn.execute(
                    "INSERT INTO users (id, username, friendly_name, email, thumb_url, is_owner, is_home, "
                    "is_active, keep_history, share_salt, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (u["user_id"], u["username"], friendly, u["email"] or None, _thumb(u["thumb"]),
                     int(bool(u["is_admin"])), int(bool(u["is_home_user"])) if has_home else 0,
                     0 if u["deleted_user"] else 1, int(u["keep_history"] if u["keep_history"] is not None else 1),
                     secrets.token_hex(8), now))
                added += 1
    return added


def _thumb(url: str | None) -> str | None:
    if url and url.startswith("https://") and ("plex.tv" in url or "plex.direct" in url):
        return url
    return None


def _ms(v) -> int | None:
    return None if v in (None, "") else int(v) // 1000


def _int(v) -> int | None:
    try:
        return None if v in (None, "") else int(v)
    except (TypeError, ValueError):
        return None


def _map_play(r: sqlite3.Row, cfg: dict, server_id: int) -> dict:
    started, stopped = int(r["h_started"]), int(r["h_stopped"] or r["h_started"])
    paused = int(r["h_paused_counter"] or 0)
    play_s = max(0, stopped - started - paused)
    media_type = r["h_media_type"] or "unknown"
    duration_s = _ms(r["m_duration"]) or _ms(r["i_duration"])
    offset_s = _ms(r["h_view_offset"]) or 0
    pct = min(100, round(offset_s / duration_s * 100)) if duration_s else None
    threshold = int(cfg.get(f"ingest.watched_pct.{media_type}", cfg.get("ingest.watched_pct.episode", 85)))
    watched = int(pct is not None and pct >= threshold)

    td = (r["i_transcode_decision"] or "").strip().lower()
    has_ts = td in ("copy", "transcode")
    video = normalise_decision(r["i_video_decision"] or r["i_stream_video_decision"])
    audio = normalise_decision(r["i_audio_decision"] or r["i_stream_audio_decision"])
    sub = normalise_decision(r["i_stream_subtitle_decision"])
    if sub in ("ignore", ""):
        sub = None
    decision = overall_decision(video, audio, sub, has_ts)
    if td == "transcode" and decision != "transcode":
        decision = "transcode"

    src = dict(container=r["i_container"] or None, video_codec=r["i_video_codec"] or None,
               resolution=normalise_resolution(r["i_video_resolution"]),
               dynamic_range=r["i_video_dynamic_range"] or None, audio_codec=r["i_audio_codec"] or None,
               audio_channels=_int(r["i_audio_channels"]), bitrate=_int(r["i_bitrate"]))
    dst = dict(container=r["i_stream_container"] or None, video_codec=r["i_stream_video_codec"] or None,
               resolution=normalise_resolution(r["i_stream_video_resolution"]),
               audio_codec=r["i_stream_audio_codec"] or None, audio_channels=_int(r["i_stream_audio_channels"]),
               bitrate=_int(r["i_stream_bitrate"]))
    reasons = infer_reasons(
        video=video, audio=audio, subtitle=sub, has_transcode_session=has_ts,
        src_container=src["container"], src_video_codec=src["video_codec"], src_resolution=src["resolution"],
        src_dynamic_range=src["dynamic_range"], src_audio_codec=src["audio_codec"],
        src_audio_channels=src["audio_channels"], src_bitrate_kbps=src["bitrate"],
        dst_container=dst["container"], dst_video_codec=dst["video_codec"], dst_resolution=dst["resolution"],
        dst_audio_codec=dst["audio_codec"], dst_audio_channels=dst["audio_channels"],
        dst_bitrate_kbps=dst["bitrate"])

    if media_type == "episode":
        thumb = r["m_grandparent_thumb"] or r["m_parent_thumb"] or r["m_thumb"]
    elif media_type == "track":
        thumb = r["m_parent_thumb"] or r["m_thumb"]
    else:
        thumb = r["m_thumb"]
    full = bool(_int(r["i_transcode_hw_full_pipeline"]))
    return {
        "server_id": server_id, "source": "tautulli", "source_id": r["h_id"],
        "user_id": r["h_user_id"], "started_at": started, "stopped_at": stopped,
        "paused_s": paused, "play_s": play_s, "start_offset_s": None, "view_offset_s": offset_s,
        "duration_s": duration_s, "percent_complete": pct, "watched": watched, "buffer_count": 0,
        "media_type": media_type, "rating_key": _int(r["h_rating_key"]),
        "parent_rating_key": _int(r["h_parent_rating_key"]),
        "grandparent_rating_key": _int(r["h_grandparent_rating_key"]),
        "guid": r["m_guid"] or None, "grandparent_guid": None, "library_id": _int(r["h_section_id"]),
        "title": r["m_title"], "parent_title": r["m_parent_title"] or None,
        "grandparent_title": r["m_grandparent_title"] or None,
        "season_index": _int(r["m_parent_media_index"]) if media_type == "episode" else None,
        "episode_index": _int(r["m_media_index"]) if media_type in ("episode", "track") else None,
        "year": _int(r["m_year"]), "thumb": thumb or None,
        "player_title": r["h_player"], "platform": r["h_platform"], "product": r["h_product"],
        "device": None, "machine_id": r["h_machine_id"], "ip_address": r["h_ip_address"] or None,
        "is_local": int((r["h_location"] or "") == "lan"), "is_relayed": int(bool(r["h_relayed"])),
        "is_secure": int(bool(r["h_secure"])), "decision": decision, "video_decision": video,
        "audio_decision": audio, "subtitle_decision": sub, "transcode_reasons": json.dumps(reasons),
        "hw_decode": int(bool(_int(r["i_transcode_hw_decoding"])) or full),
        "hw_encode": int(bool(_int(r["i_transcode_hw_encoding"])) or full),
        "src_container": src["container"], "src_video_codec": src["video_codec"],
        "src_resolution": src["resolution"], "src_dynamic_range": src["dynamic_range"],
        "src_audio_codec": src["audio_codec"], "src_audio_channels": src["audio_channels"],
        "src_bitrate_kbps": src["bitrate"], "dst_container": dst["container"],
        "dst_video_codec": dst["video_codec"], "dst_resolution": dst["resolution"],
        "dst_audio_codec": dst["audio_codec"], "dst_audio_channels": dst["audio_channels"],
        "dst_bitrate_kbps": dst["bitrate"],
        "subtitle_codec": r["i_stream_subtitle_codec"] or r["i_subtitle_codec"] or None,
        "bandwidth_kbps": _int(r["h_bandwidth"]), "quality_profile": r["h_quality_profile"] or None,
    }


def _overlaps_live(conn: sqlite3.Connection, v: dict) -> bool:
    """A live play already covers this one (both apps were running side by side)."""
    if v["rating_key"] is None:
        return False
    return conn.execute(
        "SELECT 1 FROM plays WHERE source = 'live' AND user_id = ? AND rating_key = ? "
        "AND started_at BETWEEN ? AND ? LIMIT 1",
        (v["user_id"], v["rating_key"], v["started_at"] - OVERLAP_S, v["started_at"] + OVERLAP_S),
    ).fetchone() is not None
