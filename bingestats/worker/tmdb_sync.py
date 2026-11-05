"""Read production status from TMDB into tmdb_shows (worker only). See services/tmdb.py for what's due.

A first run reads every show with a TMDB id: about 1,900 requests on Marc's
server, a few minutes at a polite pace. Rows are written in batches, so a run
that stops part way keeps what it read, and the next run picks up the rest.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

from .. import db
from ..services import settings
from ..services import tmdb as tmdb_svc
from ..tmdb import parse
from ..tmdb.client import TmdbError

log = logging.getLogger("bingestats.tmdb")

BATCH = 50
PACE_S = 0.05              # at most ~20 requests a second; TMDB allows about 50
COLS = ["tmdb_id", "tvdb_id", "name", "status", "in_production", "last_air_date", "next_air_date",
        "next_season", "last_season", "max_season", "genres", "networks", "fetched_at"]


def _write(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    with db.transaction(conn):
        conn.executemany(f"INSERT OR REPLACE INTO tmdb_shows ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})",
                         [[r[c] for c in COLS] for r in rows])


def _status(conn, ok: bool, now: int, *, error: str | None = None, info: dict | None = None) -> None:
    with db.transaction(conn):
        conn.execute("INSERT INTO arr_status (app, ok, error, last_sync_at, info) VALUES ('tmdb', ?, ?, ?, ?) "
                     "ON CONFLICT(app) DO UPDATE SET ok = excluded.ok, error = excluded.error, "
                     "last_sync_at = CASE WHEN excluded.ok = 1 THEN excluded.last_sync_at ELSE arr_status.last_sync_at END, "
                     "info = excluded.info", (int(ok), error, now if ok else None, json.dumps(info or {})))


def run(conn: sqlite3.Connection, progress=None, now: float | None = None, client=None, sleep=time.sleep) -> dict:
    now = int(now or time.time())
    client = client or tmdb_svc.client(conn)
    if client is None:
        return {"skipped": "TMDB isn't set up"}
    todo = tmdb_svc.due(conn, now)
    read = missing = 0
    rows: list[dict] = []
    try:
        for i, (tmdb_id, tvdb_id) in enumerate(todo):
            d = client.tv(tmdb_id)
            if d is None:
                rows.append(parse.missing(tmdb_id, tvdb_id, now))
                missing += 1
            else:
                rows.append(parse.tv(d, tmdb_id, tvdb_id, now))
                read += 1
            if len(rows) >= BATCH:
                _write(conn, rows)
                rows = []
                if progress:
                    progress(int((i + 1) / len(todo) * 100), f"Read {i + 1:,} of {len(todo):,} shows from TMDB")
            sleep(PACE_S)
    except TmdbError as e:
        _write(conn, rows)
        log.warning("TMDB read stopped: %s", e)
        _status(conn, False, now, error=str(e), info={"read": read, "due": len(todo)})
        settings.set(conn, "tmdb.last_run_at", now)
        raise
    _write(conn, rows)
    result = {"read": read, "not_found": missing, "due": len(todo)}
    _status(conn, True, now, info=result)
    settings.set(conn, "tmdb.last_run_at", now)
    return result
