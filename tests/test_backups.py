"""Database backups: the backup API copy, pruning, nightly schedule and the Settings page."""
from datetime import datetime
from zoneinfo import ZoneInfo

from bingestats import db
from bingestats.services import backups, jobs, settings
from bingestats.worker import jobs_runner

LDN = ZoneInfo("Europe/London")


def test_create_prune_and_paths(conn, tmp_path):
    conn.execute("INSERT INTO users (id, username, share_salt) VALUES (1, 'sarah', 's')")
    dbp = tmp_path / "test.db"
    names = [backups.create(dbp, tmp_path, keep=3, now=1_800_000_000 + i * 86400)["file"] for i in range(5)]
    listed = [b["name"] for b in backups.list_backups(tmp_path)]
    assert listed == sorted(names, reverse=True)[:3]                  # newest three kept
    copy = db.connect(backups.path_for(tmp_path, listed[0]))
    assert copy.execute("SELECT username FROM users").fetchone()[0] == "sarah"
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "wal"   # connect() sets WAL again; file was standalone
    assert backups.path_for(tmp_path, "../test.db") is None
    assert backups.path_for(tmp_path, "bingestats-20990101-000000.db") is None


def test_nightly_due(conn):
    def at(d, h, m):
        return datetime(2026, 10, d, h, m, tzinfo=LDN).timestamp()
    assert backups.nightly_due(conn, at(31, 2, 0))                     # never run: yesterday's slot is owed
    settings.set(conn, "backup.last_run_at", int(at(31, 3, 31)))
    assert not backups.nightly_due(conn, at(31, 23, 0))
    assert not backups.nightly_due(conn, at(31, 3, 28) + 86400)        # 03:28 next day
    assert backups.nightly_due(conn, at(31, 3, 31) + 86400)            # 03:31 next day


def test_backup_job(conn, tmp_path):
    settings.set(conn, "backup.keep", 2)
    jid = jobs.create(conn, "backup")
    job = jobs.claim_next(conn)

    class Cfg:
        data_dir = tmp_path
        db_path = tmp_path / "test.db"
    result = jobs_runner.run_job(conn, Cfg, job)
    assert result["file"].startswith("bingestats-") and result["size"] > 0
    assert settings.get(conn, "backup.last_run_at") > 0 and jid == job["id"]


def test_settings_backups(logged_in, app):
    data = app.config["BS_CONFIG"].data_dir
    body = logged_in.get("/settings").data.decode()
    assert "No backups yet" in body
    r = logged_in.post("/settings/backups")
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]
    b = backups.create(app.config["BS_CONFIG"].db_path, data, keep=7)
    body = logged_in.get("/settings").data.decode()
    assert b["file"] in body
    r = logged_in.get(f"/settings/backups/{b['file']}")
    assert r.status_code == 200 and r.data[:15] == b"SQLite format 3"
    assert "attachment" in r.headers["Content-Disposition"]
    assert logged_in.get("/settings/backups/..%2Fbingestats.db").status_code == 404
    assert app.test_client().get(f"/settings/backups/{b['file']}").status_code == 302   # needs login
