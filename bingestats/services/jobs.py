"""Background jobs: the web process queues them, the worker runs them.

Long work (Tautulli import, library sync) never runs in a request. A job is
a row; the worker claims the oldest queued one, reports progress, and stores
a JSON result the UI renders.
"""
from __future__ import annotations

import json
import sqlite3
import time

KINDS = {
    "tautulli_check": "Check Tautulli database",
    "tautulli_import": "Import Tautulli history",
    "library_sync": "Sync libraries",
    "user_sync": "Sync users",
    "geo": "Look up locations",
    "rebuild_binges": "Rebuild binges",
    "wrapped": "Build Wrapped",
    "backup": "Back up the database",
    "arr_sync": "Read Sonarr, Radarr and SABnzbd",
    "pin_guides": "Read episode lists for pinned shows",
}


class JobBusy(Exception):
    """A job of the same kind is already queued or running."""

    def __init__(self, job_id: int):
        super().__init__(f"Already running as job {job_id}")
        self.job_id = job_id


def create(conn: sqlite3.Connection, kind: str, params: dict | None = None, *,
           now: float | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"Unknown job kind: {kind}")
    busy = conn.execute(
        "SELECT id FROM jobs WHERE kind = ? AND status IN ('queued','running') ORDER BY id LIMIT 1", (kind,)
    ).fetchone()
    if busy:
        raise JobBusy(busy["id"])
    cur = conn.execute(
        "INSERT INTO jobs (kind, params, status, progress, created_at) VALUES (?, ?, 'queued', 0, ?)",
        (kind, json.dumps(params or {}), int(now or time.time())),
    )
    return cur.lastrowid


def ensure(conn: sqlite3.Connection, kind: str, params: dict | None = None) -> int:
    """Queue a job unless one of that kind is already pending; return its id either way."""
    try:
        return create(conn, kind, params)
    except JobBusy as e:
        return e.job_id


def claim_next(conn: sqlite3.Connection, *, now: float | None = None) -> dict | None:
    row = conn.execute(
        "UPDATE jobs SET status = 'running', started_at = ? WHERE id = "
        "(SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1) RETURNING *",
        (int(now or time.time()),),
    ).fetchone()
    return _row(row) if row else None


def progress(conn: sqlite3.Connection, job_id: int, pct: int | None, message: str | None = None) -> None:
    conn.execute("UPDATE jobs SET progress = ?, message = ? WHERE id = ?",
                 (None if pct is None else max(0, min(100, int(pct))), message, job_id))


def finish(conn: sqlite3.Connection, job_id: int, result: dict, message: str | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', progress = 100, result = ?, message = ?, finished_at = ? WHERE id = ?",
        (json.dumps(result), message, int(time.time()), job_id),
    )


def fail(conn: sqlite3.Connection, job_id: int, message: str) -> None:
    conn.execute("UPDATE jobs SET status = 'failed', message = ?, finished_at = ? WHERE id = ?",
                 (message[:1000], int(time.time()), job_id))


def fail_orphans(conn: sqlite3.Connection) -> int:
    """Jobs left 'running' by a worker that died. Called at worker start."""
    cur = conn.execute("UPDATE jobs SET status = 'failed', message = 'The worker restarted while this was running', "
                       "finished_at = ? WHERE status = 'running'", (int(time.time()),))
    return cur.rowcount


def get(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row(row) if row else None


def latest(conn: sqlite3.Connection, kind: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    return _row(row) if row else None


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d.get("params") or "{}")
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    d["label"] = KINDS.get(d["kind"], d["kind"])
    return d
