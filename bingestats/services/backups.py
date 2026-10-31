"""Database backups (SPEC.md section 17).

Uses SQLite's online backup API, so it's safe while the worker is writing.
Backups go to /data/backups/bingestats-YYYYMMDD-HHMMSS.db; the newest
`backup.keep` are kept. The worker runs one nightly at 03:30 local, and
"Backup now" in Settings queues the same job. Backups hold the Plex token,
so they're only offered for download to the signed-in admin.
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import settings

NAME = re.compile(r"^bingestats-\d{8}-\d{6}\.db$")
NIGHTLY_AT = (3, 30)


def backup_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "backups"


def create(db_path: Path, data_dir: Path, keep: int, now: float | None = None) -> dict:
    now = now or time.time()
    d = backup_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = datetime.fromtimestamp(now).strftime("bingestats-%Y%m%d-%H%M%S.db")
    final, tmp = d / name, d / (name + ".part")
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")   # one self-contained file
        ok = dst.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Backup failed its integrity check: {ok}")
    tmp.replace(final)
    removed = prune(data_dir, keep)
    return {"file": name, "size": final.stat().st_size, "removed": removed}


def prune(data_dir: Path, keep: int) -> list[str]:
    files = list_backups(data_dir)
    gone = [b["name"] for b in files[max(1, keep):]]
    for n in gone:
        (backup_dir(data_dir) / n).unlink(missing_ok=True)
    for stale in backup_dir(data_dir).glob("*.part"):   # an interrupted backup
        if time.time() - stale.stat().st_mtime > 3600:
            stale.unlink(missing_ok=True)
    return gone


def list_backups(data_dir: Path) -> list[dict]:
    d = backup_dir(data_dir)
    if not d.exists():
        return []
    out = [{"name": p.name, "size": p.stat().st_size, "created": int(p.stat().st_mtime)}
           for p in d.iterdir() if NAME.match(p.name)]
    return sorted(out, key=lambda b: b["name"], reverse=True)


def path_for(data_dir: Path, name: str) -> Path | None:
    """The backup file for a name from the list, or None (no path tricks)."""
    if not NAME.match(name or ""):
        return None
    p = backup_dir(data_dir) / name
    return p if p.is_file() else None


def nightly_due(conn: sqlite3.Connection, now: float) -> bool:
    """True once 03:30 local has passed today and no backup has run since."""
    try:
        tz = ZoneInfo(settings.get(conn, "ui.timezone"))
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("Europe/London")
    local = datetime.fromtimestamp(now, tz)
    slot = local.replace(hour=NIGHTLY_AT[0], minute=NIGHTLY_AT[1], second=0, microsecond=0)
    if local < slot:
        slot -= timedelta(days=1)
    return (settings.get(conn, "backup.last_run_at") or 0) < slot.timestamp()
