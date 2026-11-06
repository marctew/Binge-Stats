"""The API's action log (API standard section 7): every POST /api/v1/actions/<name>, with the
token's name, so "who did that" always has an answer. Shown on Settings → API tokens."""
from __future__ import annotations

import json
import sqlite3
import time

KEEP_DAYS = 90
BODY_MAX = 500


def record(conn: sqlite3.Connection, *, token_id: int | None, token_name: str, action: str, body: dict | None,
           status: int, message: str | None, now: float | None = None) -> None:
    raw = json.dumps(body if body is not None else {}, separators=(",", ":"), sort_keys=True)
    conn.execute("INSERT INTO api_audit (at, token_id, token_name, action, body, status, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (int(now or time.time()), token_id, token_name, action, raw[:BODY_MAX], status, message))


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, at, token_name, action, status, message FROM api_audit ORDER BY at DESC, id DESC LIMIT ?", (limit,))]


def prune(conn: sqlite3.Connection, now: float | None = None) -> int:
    return conn.execute("DELETE FROM api_audit WHERE at < ?", (int((now or time.time()) - KEEP_DAYS * 86400),)).rowcount
