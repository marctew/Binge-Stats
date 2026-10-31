"""Admin account. One admin, password hashed with werkzeug (scrypt)."""
from __future__ import annotations

import sqlite3
import time

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD = 8


def admin_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM admin WHERE id = 1").fetchone() is not None


def set_admin(conn: sqlite3.Connection, username: str, password: str) -> None:
    username = username.strip()
    if not username:
        raise ValueError("Username is required")
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    conn.execute(
        "INSERT INTO admin (id, username, password_hash, created_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET username = excluded.username, password_hash = excluded.password_hash",
        (username, generate_password_hash(password), int(time.time())),
    )


def check_login(conn: sqlite3.Connection, username: str, password: str) -> bool:
    row = conn.execute("SELECT username, password_hash FROM admin WHERE id = 1").fetchone()
    if row is None:
        return False
    ok = check_password_hash(row["password_hash"], password)
    return ok and row["username"].lower() == username.strip().lower()


def admin_username(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT username FROM admin WHERE id = 1").fetchone()
    return row["username"] if row else None
