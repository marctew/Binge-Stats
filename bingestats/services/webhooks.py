"""Webhooks out (API standard section 8), for Home Assistant and n8n.

Settings → Webhooks holds a URL, the event types to send (none ticked: all) and a secret. Each
event makes one delivery per matching webhook; the worker's sender POSTs it as JSON (the same
`data` as the events stream) with `X-Signature: sha256=<hex HMAC-SHA256 of the raw body>`, a 10 s
timeout, and retries after 1, 3 and 6 minutes before giving up. Deliveries never happen in a
request. The URL is a secret too (a Home Assistant webhook id is the whole credential), so after
saving it's only ever shown masked.

Notifications (services/notify.py, Apprise) are for people; these are for machines.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from typing import Callable
from urllib.parse import urlsplit

RETRY_AFTER_S = (60, 180, 360)     # after the first try: 3 retries over 10 minutes
TIMEOUT_S = 10
KEEP_DONE_S = 7 * 86400
BATCH = 20


def _types() -> list[str]:
    from .events import TYPES
    return TYPES


def validate_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("Paste a full http:// or https:// URL, e.g. http://homeassistant.lan:8123/api/webhook/binge")
    return url


def mask(url: str) -> str:
    """'http://homeassistant.lan:8123/api/webhook/abc123' -> 'http://homeassistant.lan:8123/•••'."""
    p = urlsplit(url or "")
    return f"{p.scheme}://{p.netloc}/•••" if p.netloc else "•••"


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def create(conn: sqlite3.Connection, name: str, url: str, event_types: list[str] | None = None,
           now: float | None = None) -> str:
    """Save a webhook; returns its secret, which is shown once."""
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give the webhook a name, e.g. Home Assistant")
    url = validate_url(url)
    types = [t for t in (event_types or []) if t in _types()]
    secret = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO webhooks (name, url, secret, event_types, enabled, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                 (name, url, secret, json.dumps(types), int(now or time.time())))
    return secret


def new_secret(conn: sqlite3.Connection, webhook_id: int) -> str | None:
    secret = secrets.token_urlsafe(32)
    if not conn.execute("UPDATE webhooks SET secret = ? WHERE id = ?", (secret, webhook_id)).rowcount:
        return None
    return secret


def delete(conn: sqlite3.Connection, webhook_id: int) -> bool:
    return conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,)).rowcount > 0


def list_webhooks(conn: sqlite3.Connection) -> list[dict]:
    """For Settings: never the URL or the secret in full."""
    out = []
    for r in conn.execute("SELECT * FROM webhooks ORDER BY created_at, id"):
        d = dict(r)
        d["masked_url"] = mask(d.pop("url"))
        d.pop("secret")
        d["event_types"] = json.loads(d["event_types"] or "[]")
        d["pending"] = conn.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_id = ? AND done_at IS NULL",
                                    (d["id"],)).fetchone()[0]
        out.append(d)
    return out


def enqueue(conn: sqlite3.Connection, event_id: int, type_: str, now: float) -> int:
    n = 0
    for r in conn.execute("SELECT id, event_types FROM webhooks WHERE enabled = 1"):
        wanted = json.loads(r["event_types"] or "[]")
        if not wanted or type_ in wanted:
            conn.execute("INSERT INTO webhook_deliveries (webhook_id, event_id, attempts, next_at) VALUES (?, ?, 0, ?)",
                         (r["id"], event_id, int(now)))
            n += 1
    return n


def _post(url: str, body: bytes, headers: dict) -> None:
    import requests

    from .. import __version__
    r = requests.post(url, data=body, headers={**headers, "User-Agent": f"BingeStats/{__version__}"},
                      timeout=TIMEOUT_S, allow_redirects=False)
    if not 200 <= r.status_code < 300:
        raise RuntimeError(f"HTTP {r.status_code}")


def deliver_due(conn: sqlite3.Connection, now: float | None = None,
                post: Callable[[str, bytes, dict], None] = _post) -> int:
    """Send every delivery that's due. Worker only. Returns how many were tried."""
    from . import events
    now = int(now or time.time())
    due = conn.execute(
        "SELECT d.id, d.event_id, d.attempts, w.id AS webhook_id, w.url, w.secret FROM webhook_deliveries d "
        "JOIN webhooks w ON w.id = d.webhook_id WHERE d.done_at IS NULL AND d.next_at <= ? AND w.enabled = 1 "
        "ORDER BY d.next_at, d.id LIMIT ?", (now, BATCH)).fetchall()
    for d in due:
        ev = events.get(conn, d["event_id"])
        if ev is None:
            conn.execute("UPDATE webhook_deliveries SET done_at = ?, error = 'event expired' WHERE id = ?", (now, d["id"]))
            continue
        body = json.dumps(ev, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "X-Signature": sign(body, d["secret"]),
                   "X-Event-Type": ev["type"], "X-Event-Id": str(ev["id"])}
        try:
            post(d["url"], body, headers)
        except Exception as e:  # noqa: BLE001 - any failure is a failed attempt
            err = (str(e) or e.__class__.__name__)[:200]
            attempts = d["attempts"] + 1
            if attempts > len(RETRY_AFTER_S):
                conn.execute("UPDATE webhook_deliveries SET attempts = ?, done_at = ?, error = ? WHERE id = ?",
                             (attempts, now, err, d["id"]))
            else:
                conn.execute("UPDATE webhook_deliveries SET attempts = ?, next_at = ?, error = ? WHERE id = ?",
                             (attempts, now + RETRY_AFTER_S[attempts - 1], err, d["id"]))
            conn.execute("UPDATE webhooks SET last_error = ?, last_error_at = ? WHERE id = ?", (err, now, d["webhook_id"]))
            continue
        conn.execute("UPDATE webhook_deliveries SET attempts = ?, done_at = ?, error = NULL WHERE id = ?",
                     (d["attempts"] + 1, now, d["id"]))
        conn.execute("UPDATE webhooks SET last_ok_at = ? WHERE id = ?", (now, d["webhook_id"]))
    return len(due)


def prune(conn: sqlite3.Connection, now: float | None = None) -> int:
    return conn.execute("DELETE FROM webhook_deliveries WHERE done_at IS NOT NULL AND done_at < ?",
                        (int((now or time.time()) - KEEP_DONE_S),)).rowcount
