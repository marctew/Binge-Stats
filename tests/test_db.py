import sqlite3

import pytest

from bingestats import db


def test_migrate_fresh_database_reaches_latest(tmp_path):
    c = db.connect(tmp_path / "x.db")
    assert db.current_version(c) == 0
    assert db.migrate(c) == db.LATEST
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"plays", "active_sessions", "users", "servers", "settings", "jobs"} <= tables


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == db.LATEST
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_refuses_database_from_newer_code(conn):
    conn.execute("UPDATE schema_version SET version = ?", (db.LATEST + 1,))
    with pytest.raises(RuntimeError):
        db.migrate(conn)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "x.db")

    def boom(conn):
        conn.execute("CREATE TABLE half_done (x)")
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(db, "MIGRATIONS", [boom])
    monkeypatch.setattr(db, "LATEST", 1)
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(c)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert "half_done" not in names


def test_wal_and_foreign_keys_on(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_with_lock(tmp_path):
    assert db.migrate_with_lock(tmp_path / "sub" / "y.db") == db.LATEST
