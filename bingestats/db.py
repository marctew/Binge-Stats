"""SQLite connection helper, schema and migrations.

Plain sqlite3, no ORM. Every connection gets WAL, foreign keys and a busy
timeout. Migrations are an ordered list; `migrate()` applies the ones the
database hasn't seen yet. Version 1 is the full schema from SPEC.md section 3.
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def connect(path: str | os.PathLike) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, rolling back on error. Not re-entrant."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


SCHEMA_V1 = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE admin (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  username TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL
);

CREATE TABLE api_tokens (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  scope TEXT NOT NULL CHECK (scope IN ('read','write')),
  created_at INTEGER NOT NULL, last_used_at INTEGER
);

CREATE TABLE servers (
  id INTEGER PRIMARY KEY, machine_id TEXT UNIQUE, name TEXT, version TEXT,
  last_seen_at INTEGER, is_up INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT, friendly_name TEXT,
  email TEXT, thumb_url TEXT,
  is_owner INTEGER NOT NULL DEFAULT 0, is_home INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  keep_history INTEGER NOT NULL DEFAULT 1,
  max_streams INTEGER, max_transcodes INTEGER,
  limit_action TEXT NOT NULL DEFAULT 'notify' CHECK (limit_action IN ('notify','stop')),
  limit_message TEXT,
  share_salt TEXT NOT NULL,
  first_seen_at INTEGER, last_seen_at INTEGER
);

CREATE TABLE libraries (
  id INTEGER PRIMARY KEY,
  server_id INTEGER NOT NULL REFERENCES servers(id),
  name TEXT NOT NULL, type TEXT NOT NULL,
  item_count INTEGER, child_count INTEGER, total_size_bytes INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1, synced_at INTEGER
);

CREATE TABLE media_items (
  rating_key INTEGER PRIMARY KEY,
  server_id INTEGER NOT NULL, library_id INTEGER REFERENCES libraries(id),
  type TEXT NOT NULL,
  guid TEXT,
  parent_rating_key INTEGER, grandparent_rating_key INTEGER,
  title TEXT NOT NULL, parent_title TEXT, grandparent_title TEXT,
  season_index INTEGER, episode_index INTEGER, year INTEGER,
  duration_s INTEGER, added_at INTEGER, originally_available_at TEXT,
  thumb TEXT, art TEXT,
  size_bytes INTEGER,
  video_codec TEXT, video_resolution TEXT, video_dynamic_range TEXT,
  audio_codec TEXT, audio_channels INTEGER, container TEXT, bitrate_kbps INTEGER,
  is_present INTEGER NOT NULL DEFAULT 1,
  synced_at INTEGER
);
CREATE INDEX media_items_guid ON media_items(guid);
CREATE INDEX media_items_parent ON media_items(parent_rating_key);
CREATE INDEX media_items_grandparent ON media_items(grandparent_rating_key);

CREATE TABLE active_sessions (
  session_id TEXT PRIMARY KEY,
  session_key TEXT, server_id INTEGER NOT NULL,
  user_id INTEGER, rating_key INTEGER, media_type TEXT,
  started_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL, missed_polls INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,
  state_since INTEGER NOT NULL,
  paused_s INTEGER NOT NULL DEFAULT 0,
  buffer_count INTEGER NOT NULL DEFAULT 0,
  start_offset_s INTEGER, view_offset_s INTEGER, duration_s INTEGER,
  snapshot TEXT NOT NULL
);

CREATE TABLE plays (
  id INTEGER PRIMARY KEY,
  server_id INTEGER NOT NULL,
  group_id INTEGER REFERENCES plays(id),
  source TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live','tautulli')),
  source_id INTEGER,
  session_id TEXT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  started_at INTEGER NOT NULL, stopped_at INTEGER NOT NULL,
  paused_s INTEGER NOT NULL DEFAULT 0,
  play_s INTEGER NOT NULL,
  start_offset_s INTEGER, view_offset_s INTEGER, duration_s INTEGER,
  percent_complete INTEGER, watched INTEGER NOT NULL DEFAULT 0,
  buffer_count INTEGER NOT NULL DEFAULT 0,
  media_type TEXT NOT NULL, rating_key INTEGER, parent_rating_key INTEGER, grandparent_rating_key INTEGER,
  guid TEXT, grandparent_guid TEXT, library_id INTEGER,
  title TEXT, parent_title TEXT, grandparent_title TEXT,
  season_index INTEGER, episode_index INTEGER, year INTEGER, thumb TEXT,
  player_title TEXT, platform TEXT, product TEXT, device TEXT, machine_id TEXT,
  ip_address TEXT, is_local INTEGER, is_relayed INTEGER, is_secure INTEGER,
  decision TEXT,
  video_decision TEXT, audio_decision TEXT, subtitle_decision TEXT,
  transcode_reasons TEXT,
  hw_decode INTEGER, hw_encode INTEGER,
  src_container TEXT, src_video_codec TEXT, src_resolution TEXT, src_dynamic_range TEXT,
  src_audio_codec TEXT, src_audio_channels INTEGER, src_bitrate_kbps INTEGER,
  dst_container TEXT, dst_video_codec TEXT, dst_resolution TEXT,
  dst_audio_codec TEXT, dst_audio_channels INTEGER, dst_bitrate_kbps INTEGER,
  subtitle_codec TEXT, bandwidth_kbps INTEGER, quality_profile TEXT,
  UNIQUE (source, source_id)
);
CREATE INDEX plays_user_started ON plays(user_id, started_at);
CREATE INDEX plays_started ON plays(started_at);
CREATE INDEX plays_guid ON plays(guid);
CREATE INDEX plays_grandparent_guid ON plays(grandparent_guid);
CREATE INDEX plays_rating_key ON plays(rating_key);
CREATE INDEX plays_library ON plays(library_id, started_at);
CREATE INDEX plays_group ON plays(group_id);

CREATE TABLE ip_geo (
  ip TEXT PRIMARY KEY, is_private INTEGER NOT NULL,
  city TEXT, region TEXT, country_code TEXT, country TEXT, lat REAL, lon REAL,
  looked_up_at INTEGER NOT NULL
);

CREATE TABLE binges (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, grandparent_guid TEXT NOT NULL,
  show_title TEXT, started_at INTEGER NOT NULL, ended_at INTEGER NOT NULL,
  episode_count INTEGER NOT NULL, play_s INTEGER NOT NULL,
  first_play_id INTEGER NOT NULL, last_play_id INTEGER NOT NULL
);
CREATE INDEX binges_user ON binges(user_id, started_at);

CREATE TABLE alerts (
  id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL,
  kind TEXT NOT NULL,
  user_id INTEGER, detail TEXT NOT NULL,
  acknowledged INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE notify_channels (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, apprise_url TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE notify_rules (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  trigger TEXT NOT NULL, conditions TEXT NOT NULL DEFAULT '[]',
  title_template TEXT, body_template TEXT NOT NULL,
  channel_ids TEXT NOT NULL,
  cooldown_s INTEGER NOT NULL DEFAULT 0, include_poster INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE notify_log (
  id INTEGER PRIMARY KEY, rule_id INTEGER, channel_id INTEGER, sent_at INTEGER NOT NULL,
  dedupe_key TEXT, ok INTEGER NOT NULL, error TEXT, title TEXT, body TEXT
);
CREATE INDEX notify_log_dedupe ON notify_log(rule_id, dedupe_key, sent_at);

CREATE TABLE recently_added_queue (
  rating_key INTEGER PRIMARY KEY, queued_at INTEGER NOT NULL, processed_at INTEGER
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
  progress INTEGER, message TEXT, result TEXT,
  created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER
);

CREATE TABLE wrapped_cache (
  user_id INTEGER NOT NULL,
  year INTEGER NOT NULL, computed_at INTEGER NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY (user_id, year)
);
"""


def _m1(conn: sqlite3.Connection) -> None:
    _run_script_in_tx(conn, SCHEMA_V1)


# Ordered. Index + 1 is the schema version the migration produces. Never edit
# a released migration; append a new one and a test that upgrades from the
# previous version.
def _m2(conn: sqlite3.Connection) -> None:
    """Notifications (M4): the last real event per trigger, for template
    previews, and the shipped default rules (disabled, no channels)."""
    import json
    conn.execute("CREATE TABLE notify_samples (trigger TEXT PRIMARY KEY, context TEXT NOT NULL, at INTEGER NOT NULL)")
    conn.execute("ALTER TABLE notify_rules ADD COLUMN created_at INTEGER")
    from .services.notify_defaults import DEFAULT_RULES
    for r in DEFAULT_RULES:
        conn.execute(
            "INSERT INTO notify_rules (name, enabled, trigger, conditions, title_template, body_template, channel_ids, "
            "cooldown_s, include_poster, created_at) VALUES (?, 0, ?, ?, ?, ?, '[]', ?, ?, strftime('%s','now'))",
            (r["name"], r["trigger"], json.dumps(r.get("conditions", [])), r["title"], r["body"],
             r.get("cooldown_s", 0), int(r.get("include_poster", True))))


def _m3(conn: sqlite3.Connection) -> None:
    """Stream limits and sharing (M5): two more default rules, disabled."""
    from .services.notify_defaults import LIMIT_RULES
    for r in LIMIT_RULES:
        conn.execute(
            "INSERT INTO notify_rules (name, enabled, trigger, conditions, title_template, body_template, channel_ids, "
            "cooldown_s, include_poster, created_at) VALUES (?, 0, ?, '[]', ?, ?, '[]', ?, ?, strftime('%s','now'))",
            (r["name"], r["trigger"], r["title"], r["body"], r.get("cooldown_s", 0), int(r.get("include_poster", True))))
    conn.execute("CREATE INDEX IF NOT EXISTS alerts_user ON alerts(user_id, kind, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS alerts_open ON alerts(acknowledged, created_at)")


def _m4(conn: sqlite3.Connection) -> None:
    """Insights (M6): fill the binges table from existing history."""
    conn.execute("CREATE INDEX IF NOT EXISTS binges_show ON binges(grandparent_guid)")
    conn.execute("CREATE INDEX IF NOT EXISTS plays_user_type ON plays(user_id, media_type, started_at)")
    has_history = conn.execute("SELECT 1 FROM plays WHERE media_type = 'episode' LIMIT 1").fetchone()
    pending = conn.execute("SELECT 1 FROM jobs WHERE kind = 'rebuild_binges' AND status IN ('queued','running')").fetchone()
    if has_history and not pending:
        conn.execute("INSERT INTO jobs (kind, params, status, progress, created_at) "
                     "VALUES ('rebuild_binges', '{}', 'queued', 0, strftime('%s','now'))")


def _queue_rebuild(conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT 1 FROM jobs WHERE kind = 'rebuild_binges' AND status = 'queued'").fetchone():
        conn.execute("INSERT INTO jobs (kind, params, status, progress, created_at) "
                     "VALUES ('rebuild_binges', '{}', 'queued', 0, strftime('%s','now'))")


def _m5(conn: sqlite3.Connection) -> None:
    """Show guids for episode plays of shows that were re-added to Plex (new
    rating keys), found through the episode's guid. Then recount binges."""
    from .services.libraries import backfill_play_guids
    if backfill_play_guids(conn):
        _queue_rebuild(conn)


def _m6(conn: sqlite3.Connection) -> None:
    """Same backfill, now also matching legacy-agent plays by show title and
    episode number."""
    _m5(conn)


MIGRATIONS = [_m1, _m2, _m3, _m4, _m5, _m6]
LATEST = len(MIGRATIONS)


def current_version(conn: sqlite3.Connection) -> int:
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not has:
        return 0
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return row[0] if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to LATEST. Returns the version it ended on."""
    version = current_version(conn)
    if version > LATEST:
        raise RuntimeError(
            f"Database is schema version {version}, this code knows up to {LATEST}. "
            "Update Binge Stats before using this database."
        )
    for target in range(version + 1, LATEST + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            MIGRATIONS[target - 1](conn)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (target,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return current_version(conn)


def _run_script_in_tx(conn: sqlite3.Connection, script: str) -> None:
    # executescript() would COMMIT the open transaction first, so run the
    # statements one by one to keep the migration atomic.
    for stmt in _split_sql(script):
        conn.execute(stmt)


def _split_sql(script: str) -> list[str]:
    out, buf = [], []
    for line in script.splitlines():
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def migrate_with_lock(path: str | os.PathLike) -> int:
    """Migrate under a file lock so web and worker starting together don't race."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".migrate.lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        conn = connect(path)
        try:
            return migrate(conn)
        finally:
            conn.close()
