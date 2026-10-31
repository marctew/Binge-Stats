"""Sign-ins for the people on the server ("viewers"), set up by the admin.

A viewer signs in with their Plex username and a password the admin gives
them, and sees only their own stats (web/routes_me.py). The username isn't
stored: it's read from users.username at sign-in, so it follows renames on
Plex. Managed Home users have no Plex username and can't have a login.
Removing someone from Plex sharing (users.is_active = 0) stops their login.
"""
from __future__ import annotations

import sqlite3
import time

from werkzeug.security import check_password_hash, generate_password_hash

from .auth import MIN_PASSWORD, admin_username


def get(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM user_logins WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def ids_with_login(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT user_id FROM user_logins")}


def can_have_login(conn: sqlite3.Connection, user_id: int) -> tuple[bool, str]:
    """(ok, reason when not)."""
    u = conn.execute("SELECT username, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    if u is None:
        return False, "No such user"
    if not (u["username"] or "").strip():
        return False, "They have no Plex username (a managed Home user), so there's nothing to sign in with."
    if not u["is_active"]:
        return False, "They no longer have access to your Plex server."
    admin = admin_username(conn)
    if admin and admin.lower() == u["username"].lower():
        return False, f"Their Plex username is the same as your admin login ({admin}). Change one of them first."
    clash = conn.execute("SELECT COUNT(*) FROM users WHERE lower(username) = lower(?) AND is_active = 1",
                         (u["username"],)).fetchone()[0]
    if clash > 1:
        return False, "Another person on the server has the same username."
    return True, ""


def set_password(conn: sqlite3.Connection, user_id: int, password: str, now: float | None = None) -> None:
    """Create the login, or reset its password (which signs them out everywhere)."""
    ok, why = can_have_login(conn, user_id)
    if not ok:
        raise ValueError(why)
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    now = int(now or time.time())
    conn.execute(
        "INSERT INTO user_logins (user_id, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET password_hash = excluded.password_hash, "
        "updated_at = MAX(excluded.updated_at, user_logins.updated_at + 1)",   # always moves, so old sessions end
        (user_id, generate_password_hash(password), now, now))


def remove(conn: sqlite3.Connection, user_id: int) -> bool:
    return conn.execute("DELETE FROM user_logins WHERE user_id = ?", (user_id,)).rowcount > 0


def check(conn: sqlite3.Connection, username: str, password: str, now: float | None = None) -> dict | None:
    """The login for these credentials, or None. Records the sign-in time."""
    name = (username or "").strip()
    if not name:
        return None
    rows = conn.execute(
        "SELECT l.*, u.username FROM user_logins l JOIN users u ON u.id = l.user_id "
        "WHERE lower(u.username) = lower(?) AND u.is_active = 1", (name,)).fetchall()
    if len(rows) != 1 or not check_password_hash(rows[0]["password_hash"], password):
        return None
    conn.execute("UPDATE user_logins SET last_login_at = ? WHERE user_id = ?", (int(now or time.time()), rows[0]["user_id"]))
    return dict(rows[0])


def session_still_valid(conn: sqlite3.Connection, user_id: int, version: int | None) -> bool:
    """False once the login is removed, its password reset, or the person loses access."""
    row = conn.execute("SELECT l.updated_at FROM user_logins l JOIN users u ON u.id = l.user_id "
                       "WHERE l.user_id = ? AND u.is_active = 1", (user_id,)).fetchone()
    return row is not None and row["updated_at"] == version


def change_own_password(conn: sqlite3.Connection, user_id: int, current: str, new: str) -> int:
    """Returns the new session version so the person stays signed in here."""
    row = conn.execute("SELECT password_hash FROM user_logins WHERE user_id = ?", (user_id,)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], current):
        raise ValueError("Your current password isn't right")
    if len(new) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    now = int(time.time())
    version = conn.execute("SELECT updated_at FROM user_logins WHERE user_id = ?", (user_id,)).fetchone()[0]
    now = max(now, (version or 0) + 1)
    conn.execute("UPDATE user_logins SET password_hash = ?, updated_at = ? WHERE user_id = ?",
                 (generate_password_hash(new), now, user_id))
    return now
