"""API tokens: created in Settings, shown once, stored as sha256."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

PREFIX = "bs_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(conn: sqlite3.Connection, name: str, scope: str) -> str:
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give the token a name, e.g. Home Assistant")
    if scope not in ("read", "write"):
        raise ValueError("Scope must be read or write")
    token = PREFIX + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO api_tokens (name, token_hash, scope, created_at) VALUES (?, ?, ?, ?)",
                 (name, _hash(token), scope, int(time.time())))
    return token


def check(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute("SELECT id, name, scope FROM api_tokens WHERE token_hash = ?", (_hash(token.strip()),)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (int(time.time()), row["id"]))
    return dict(row)


def list_tokens(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, scope, created_at, last_used_at FROM api_tokens ORDER BY created_at DESC")]


def revoke(conn: sqlite3.Connection, token_id: int) -> None:
    conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
