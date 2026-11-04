"""Typed access to the settings table. Nothing else reads it directly."""
from __future__ import annotations

import copy
import json
import sqlite3
from typing import Any

DEFAULTS: dict[str, Any] = {
    "plex.url": "",
    "plex.token": "",
    "plex.verify_tls": True,
    "plex.client_id": "",
    "plex.pending_token": "",      # token from PIN sign-in, before a server is chosen
    "poll.interval_s": 5,
    "poll.use_websocket": True,
    "ingest.min_play_s": 30,
    "ingest.grace_polls": 3,
    "ingest.watched_pct.movie": 85,
    "ingest.watched_pct.episode": 85,
    "ingest.watched_pct.track": 50,
    "ingest.group_resume_h": 24,
    "binge.gap_min": 30,
    "binge.min_episodes": 3,
    "abandoned.days": 60,
    "library.sync_h": 6,
    "library.stale_months": 12,
    "library.last_sync_at": 0,
    "library.recent_check_min": 10,
    "library.last_recent_check_at": 0,
    "notify.digest_wait_min": 10,
    "users.last_sync_at": 0,
    "geo.enabled": True,
    "geo.last_run_at": 0,
    "wrapped.server_salt": "",     # rotated to revoke the server-wide Wrapped links
    "wrapped.last_run_at": 0,
    "map.tile_url": "",            # blank = OpenStreetMap's standard tiles (no key needed)
    "map.tile_attribution": "",
    "ui.timezone": "Europe/London",
    "ui.theme": "dark",
    "ui.public_url": "",           # e.g. https://binge.example.com; share links use it when set
    "mqtt.enabled": False,
    "mqtt.host": "",
    "mqtt.port": 1883,
    "mqtt.username": "",
    "mqtt.password": "",
    "mqtt.tls": False,
    "mqtt.base_topic": "bingestats",
    "mqtt.discovery_prefix": "homeassistant",
    "mqtt.tracked_users": [],      # user ids that get a "watching" sensor and a stop button
    "sonarr.url": "", "sonarr.api_key": "", "sonarr.link_url": "",   # link_url: address for "Open in" buttons
    "radarr.url": "", "radarr.api_key": "", "radarr.link_url": "",
    "sabnzbd.url": "", "sabnzbd.api_key": "",
    "arr.sync_h": 3,
    "arr.last_sync_at": 0,
    "backup.keep": 7,
    "backup.last_run_at": 0,
    "auth.proxy_header": "",
    # Written by the worker, read by the UI.
    "worker.heartbeat_at": 0,
    "worker.last_error": "",
    "mqtt.status": "off",
    "mqtt.published_users": [],    # users whose discovery is out there, so untracking can remove it
}

# Keys never shown back in the UI or logged.
SECRET_KEYS = {"plex.token", "plex.pending_token", "mqtt.password", "wrapped.server_salt",
               "sonarr.api_key", "radarr.api_key", "sabnzbd.api_key"}

# Bounds for numeric settings edited in the UI.
BOUNDS: dict[str, tuple[int, int]] = {
    "poll.interval_s": (2, 30),
    "ingest.min_play_s": (0, 600),
    "ingest.grace_polls": (1, 20),
    "ingest.watched_pct.movie": (1, 100),
    "ingest.watched_pct.episode": (1, 100),
    "ingest.watched_pct.track": (1, 100),
    "ingest.group_resume_h": (0, 168),
    "library.sync_h": (1, 168),
    "library.recent_check_min": (2, 120),
    "notify.digest_wait_min": (0, 240),
    "mqtt.port": (1, 65535),
    "binge.gap_min": (5, 240),
    "binge.min_episodes": (2, 20),
    "abandoned.days": (7, 730),
    "library.stale_months": (1, 60),
    "backup.keep": (1, 60),
    "arr.sync_h": (1, 48),
}


def get(conn: sqlite3.Connection, key: str) -> Any:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return copy.deepcopy(DEFAULTS[key])
    return json.loads(row[0])


def get_many(conn: sqlite3.Connection, prefix: str = "") -> dict[str, Any]:
    out = {k: copy.deepcopy(v) for k, v in DEFAULTS.items() if k.startswith(prefix)}
    for row in conn.execute("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",)):
        if row[0] in DEFAULTS:
            out[row[0]] = json.loads(row[1])
    return out


def set(conn: sqlite3.Connection, key: str, value: Any) -> None:  # noqa: A001 - mirrors get()
    if key not in DEFAULTS:
        raise KeyError(key)
    default = DEFAULTS[key]
    if isinstance(default, bool):
        value = bool(value)
    elif isinstance(default, int):
        value = int(value)
        if key in BOUNDS:
            lo, hi = BOUNDS[key]
            if not lo <= value <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
    elif isinstance(default, str):
        value = str(value)
    elif isinstance(default, list):   # lists are of ids
        value = sorted({int(v) for v in value})
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def plex_configured(conn: sqlite3.Connection) -> bool:
    return bool(get(conn, "plex.url") and get(conn, "plex.token"))
