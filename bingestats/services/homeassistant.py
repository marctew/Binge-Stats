"""Settings → Home Assistant: the MQTT connection and which users get entities.

The worker does the publishing (worker/mqtt.py); this only reads and saves
settings, so the web process never talks to the broker.
"""
from __future__ import annotations

import re
import sqlite3

from . import settings

TOPIC_KEYS = ("mqtt.base_topic", "mqtt.discovery_prefix")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*$")


def valid_topic(t: str) -> bool:
    """A plain topic prefix: no wildcards, no empty levels, no leading/trailing slash."""
    return bool(TOPIC_RE.match(t or ""))


def view(conn: sqlite3.Connection) -> dict:
    cfg = settings.get_many(conn, "mqtt.")
    has_password = bool(cfg.pop("mqtt.password"))
    users = [dict(r) for r in conn.execute(
        "SELECT id, username, friendly_name, is_owner FROM users WHERE is_active = 1 "
        "ORDER BY is_owner DESC, COALESCE(friendly_name, username) COLLATE NOCASE")]
    tracked = set(cfg["mqtt.tracked_users"])
    for u in users:
        u["name"] = u["friendly_name"] or u["username"]
        u["tracked"] = u["id"] in tracked
    return {**cfg, "has_password": has_password, "users": users}


def save(conn: sqlite3.Connection, form) -> None:
    """`form` is a request.form-like mapping. A blank password keeps the saved
    one; tick "clear_password" to remove it. Raises ValueError with a message
    for the page, and saves nothing in that case."""
    enabled = form.get("mqtt.enabled") == "on"
    host = (form.get("mqtt.host") or "").strip()
    if enabled and not host:
        raise ValueError("Enter the broker's host or IP address")
    try:
        port = int(form.get("mqtt.port") or 1883)
    except ValueError:
        raise ValueError("Port must be a number") from None
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    topics = {}
    for k in TOPIC_KEYS:
        v = (form.get(k) or settings.DEFAULTS[k]).strip()
        if not valid_topic(v):
            raise ValueError(f"'{v}' isn't a usable topic: letters, numbers, - and _ separated by /")
        topics[k] = v
    try:
        tracked = sorted({int(x) for x in form.getlist("tracked_users")})
    except ValueError:
        raise ValueError("Unknown user") from None
    known = {r[0] for r in conn.execute("SELECT id FROM users")}
    if set(tracked) - known:
        raise ValueError("Unknown user")

    settings.set(conn, "mqtt.enabled", enabled)
    settings.set(conn, "mqtt.host", host)
    settings.set(conn, "mqtt.port", port)
    settings.set(conn, "mqtt.username", (form.get("mqtt.username") or "").strip())
    if form.get("clear_password") == "on":
        settings.set(conn, "mqtt.password", "")
    elif form.get("mqtt.password"):
        settings.set(conn, "mqtt.password", form.get("mqtt.password"))
    settings.set(conn, "mqtt.tls", form.get("mqtt.tls") == "on")
    for k, v in topics.items():
        settings.set(conn, k, v)
    settings.set(conn, "mqtt.tracked_users", tracked)
