"""Runs queued jobs on their own thread and connection, so a long import or
library sync never delays session polling."""
from __future__ import annotations

import logging
import threading
import time

from .. import db
from ..importers import tautulli
from ..plex.client import PlexError
from ..services import activity, backups, binges, geo, jobs, settings, wrapped
from .library_sync import sync_libraries, sync_users

log = logging.getLogger(__name__)


class JobRunner(threading.Thread):
    def __init__(self, cfg, stop: threading.Event, wake: threading.Event):
        super().__init__(name="jobs", daemon=True)
        self.cfg = cfg
        self.stop_event = stop
        self.wake = wake
        self.current: int | None = None

    def run(self) -> None:
        conn = db.connect(self.cfg.db_path)
        n = jobs.fail_orphans(conn)
        if n:
            log.warning("marked %d interrupted job(s) as failed", n)
        while not self.stop_event.is_set():
            job = jobs.claim_next(conn)
            if job is None:
                self.wake.wait(2)
                self.wake.clear()
                continue
            self.current = job["id"]
            log.info("job %d %s started", job["id"], job["kind"])
            t0 = time.monotonic()
            try:
                result = run_job(conn, self.cfg, job)
            except (tautulli.TautulliImportError, PlexError, LookupError) as e:
                jobs.fail(conn, job["id"], str(e))
                log.warning("job %d %s failed: %s", job["id"], job["kind"], e)
            except Exception as e:  # noqa: BLE001
                log.exception("job %d crashed", job["id"])
                jobs.fail(conn, job["id"], f"{e.__class__.__name__}: {e}")
            else:
                jobs.finish(conn, job["id"], result)
                log.info("job %d %s done in %.1fs: %s", job["id"], job["kind"], time.monotonic() - t0,
                         {k: v for k, v in result.items() if not isinstance(v, (list, dict))})
            self.current = None
        conn.close()


def run_job(conn, cfg, job: dict) -> dict:
    jid, kind, params = job["id"], job["kind"], job["params"]

    def progress(pct: int, message: str) -> None:
        jobs.progress(conn, jid, pct, message)

    if kind == "tautulli_check":
        return tautulli.inspect(conn, params["path"], cfg.data_dir / "tmp")
    if kind == "tautulli_import":
        result = tautulli.run(conn, params["path"], cfg.data_dir / "tmp", progress)
        if settings.get(conn, "geo.enabled"):
            jobs.ensure(conn, "geo")
        jobs.ensure(conn, "rebuild_binges")
        wrapped.clear(conn)
        return result
    if kind == "geo":
        return run_geo(conn, cfg, progress)
    if kind == "rebuild_binges":
        with db.transaction(conn):
            result = binges.rebuild(conn, progress)
            wrapped.clear(conn)   # longest binges may have changed
        return result
    if kind == "arr_sync":
        from . import arr_sync
        result = arr_sync.run(conn, progress)
        settings.set(conn, "arr.last_sync_at", int(time.time()))
        return result
    if kind == "downloads_refresh":
        from .downloads import REFRESH
        REFRESH.set()                    # the watcher's next read is a full one, straight away
        return {"ok": True}
    if kind == "backup":
        result = backups.create(cfg.db_path, cfg.data_dir, int(settings.get(conn, "backup.keep")))
        settings.set(conn, "backup.last_run_at", int(time.time()))
        return result
    if kind == "wrapped":
        with db.transaction(conn):
            result = wrapped.refresh(conn, params.get("year"), progress)
            settings.set(conn, "wrapped.last_run_at", int(time.time()))
        return result

    client = activity.plex_client(conn)
    if client is None:
        raise LookupError("Plex is not connected")
    if kind == "library_sync":
        row = conn.execute("SELECT id FROM servers ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise LookupError("Waiting for the first successful poll of Plex")
        return sync_libraries(conn, client, row["id"], progress)
    if kind == "user_sync":
        return sync_users(conn, client, progress)
    raise LookupError(f"Unknown job kind {kind}")


def run_geo(conn, cfg, progress) -> dict:
    result = {}
    if geo.needs_download(cfg.data_dir):
        progress(5, "Downloading the DB-IP city database (about 60 MB)")
        result.update(geo.download(cfg.data_dir))
    progress(60, "Looking up IP addresses")
    result.update(geo.fill(conn, cfg.data_dir))
    settings.set(conn, "geo.last_run_at", int(time.time()))
    return result


def schedule_due(conn, now: float | None = None) -> list[int]:
    """Queue scheduled jobs that are due. Called from the worker loop."""
    now = now or time.time()
    queued = []
    # Backups: nightly at 03:30 local, Plex or not.
    if backups.nightly_due(conn, now):
        queued.append(jobs.ensure(conn, "backup"))
    # Sonarr / Radarr / SABnzbd snapshots, if any are set up. They don't need Plex.
    from ..services import arr as arr_svc
    if any(arr_svc.configured(conn).values()) and \
            now - (settings.get(conn, "arr.last_sync_at") or 0) >= int(settings.get(conn, "arr.sync_h")) * 3600:
        queued.append(jobs.ensure(conn, "arr_sync"))
    if not settings.plex_configured(conn):
        return queued
    if conn.execute("SELECT 1 FROM servers LIMIT 1").fetchone() is None:
        return queued
    every = max(1, int(settings.get(conn, "library.sync_h"))) * 3600
    if now - (settings.get(conn, "library.last_sync_at") or 0) >= every:
        queued.append(jobs.ensure(conn, "library_sync"))
    if now - (settings.get(conn, "users.last_sync_at") or 0) >= every:
        queued.append(jobs.ensure(conn, "user_sync"))
    # Geo: new IPs at most hourly; the database itself refreshes monthly.
    if settings.get(conn, "geo.enabled") and now - (settings.get(conn, "geo.last_run_at") or 0) >= 3600:
        from pathlib import Path
        data_dir = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
        if geo.pending(conn) or geo.needs_download(data_dir, now):
            queued.append(jobs.ensure(conn, "geo"))
    # Wrapped: this year's pages, rebuilt daily.
    if now - (settings.get(conn, "wrapped.last_run_at") or 0) >= 86400 and \
            conn.execute("SELECT 1 FROM plays LIMIT 1").fetchone():
        queued.append(jobs.ensure(conn, "wrapped"))
    return queued
