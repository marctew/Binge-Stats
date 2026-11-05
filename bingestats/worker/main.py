"""Worker process: the only writer of sessions, plays, users and library data.

One loop: poll Plex on an interval (or straight away when the websocket says
playback changed), write a heartbeat, and re-read settings each pass so
changes made in the UI apply without a restart.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
import uuid

from .. import config as config_mod
from .. import db
from ..plex.client import PlexAuthError, PlexClient
from ..plex.ws import PlexNudger
from ..services import activity, settings
from ..services.notify import Notifier
from .jobs_runner import JobRunner, schedule_due
from .downloads import DownloadWatcher
from .mqtt import MqttBridge
from .poller import Poller
from ..services import pin_notify
from .recently_added import check_recent, digest

log = logging.getLogger("bingestats.worker")


def log_event(trigger: str, ctx: dict) -> None:
    s = ctx.get("session") or {}
    who = s.get("user_title") or s.get("user_id") or ""
    what = s.get("grandparent_title") or s.get("title") or ""
    if s.get("grandparent_title") and s.get("title"):
        what = f"{s['grandparent_title']} - {s['title']}"
    log.info("%s %s %s", trigger, who, what)


def ensure_client_id(conn) -> str:
    cid = settings.get(conn, "plex.client_id")
    if not cid:
        cid = str(uuid.uuid4())
        settings.set(conn, "plex.client_id", cid)
    return cid


class Worker:
    def __init__(self, cfg: config_mod.Config):
        self.cfg = cfg
        self.stop = threading.Event()
        self.nudge = threading.Event()
        self.conn = db.connect(cfg.db_path)
        self.poller: Poller | None = None
        self.nudger: PlexNudger | None = None
        self._plex_key: tuple | None = None
        self.job_wake = threading.Event()
        self.jobs = JobRunner(cfg, self.stop, self.job_wake)
        self.client: PlexClient | None = None
        self.notifier = Notifier(self.conn, cfg.db_path, poster_fetch=self._poster)
        self.downloads = DownloadWatcher(cfg.db_path, self.stop)
        self.mqtt = MqttBridge(self.conn, stop_user=self._stop_user, wake=self.nudge.set)

    def _stop_user(self, user_id: int) -> int:
        return activity.stop_user_sessions(self.conn, user_id, client=self.client)

    def _poster(self, path: str) -> bytes | None:
        if self.client is None:
            return None
        r = self.client.photo(path, 600, 900)
        return r.content if r.headers.get("Content-Type", "").startswith("image/") else None

    def on_event(self, trigger: str, ctx: dict) -> None:
        log_event(trigger, ctx)
        self.notifier.handle(trigger, ctx)

    def recently_added(self) -> None:
        """Quick new-item check when due, then fire any digest that's ready."""
        now = time.time()
        every = int(settings.get(self.conn, "library.recent_check_min")) * 60
        if self.client is not None and self.poller is not None and self.poller.server_id is not None \
                and now - (settings.get(self.conn, "library.last_recent_check_at") or 0) >= every:
            n = check_recent(self.conn, self.client, self.poller.server_id, now)
            if n:
                log.info("recent check queued %d new item(s)", n)
        for event in digest(self.conn, now):
            self.on_event("recently_added", event)
            for pin_event in pin_notify.arrivals(self.conn, event, now):
                self.on_event(pin_notify.ARRIVED, pin_event)
        for trigger, pin_event in pin_notify.pending(self.conn, now):     # air dates moved (from the Sonarr sync)
            self.on_event(trigger, pin_event)
        if pin_notify.week_due(self.conn, now):
            for pin_event in pin_notify.week_events(self.conn, now):
                self.on_event(pin_notify.WEEK, pin_event)

    def _setup_plex(self) -> bool:
        url = settings.get(self.conn, "plex.url")
        token = settings.get(self.conn, "plex.token")
        verify = settings.get(self.conn, "plex.verify_tls")
        use_ws = settings.get(self.conn, "poll.use_websocket")
        key = (url, token, verify, use_ws)
        if key == self._plex_key and self.poller is not None:
            return True
        self._teardown_ws()
        self.poller = None
        self.client = None
        self._plex_key = key
        if not (url and token):
            return False
        client_id = ensure_client_id(self.conn)
        client = PlexClient(url, token, client_id, verify_tls=verify)
        self.client = client
        self.poller = Poller(self.conn, client, on_event=self.on_event, data_dir=self.cfg.data_dir)
        if use_ws:
            self.nudger = PlexNudger(client.websocket_url(), token, client_id, self.nudge, self.stop)
            self.nudger.start()
        log.info("Polling Plex at %s", url)
        return True

    def _teardown_ws(self) -> None:
        if self.nudger is not None:
            self.nudger.close()
            self.nudger = None

    def heartbeat(self, error: str = "") -> None:
        settings.set(self.conn, "worker.heartbeat_at", int(time.time()))
        if settings.get(self.conn, "worker.last_error") != error:
            settings.set(self.conn, "worker.last_error", error)

    def run(self) -> None:
        log.info("Worker started")
        self.jobs.start()
        self.downloads.start()
        while not self.stop.is_set():
            error = ""
            try:
                if self._setup_plex():
                    result = self.poller.poll_once()
                    if not result.ok:
                        error = "Cannot reach Plex"
                else:
                    error = "Plex is not configured"
            except PlexAuthError as e:
                error = str(e)
            except Exception as e:  # noqa: BLE001 - keep the worker alive, surface the error
                log.exception("poll failed")
                error = f"{e.__class__.__name__}: {e}"
            try:
                self.heartbeat(error)
                if schedule_due(self.conn):
                    self.job_wake.set()
            except Exception:  # noqa: BLE001
                log.exception("heartbeat or scheduling failed")
            try:
                self.recently_added()
            except Exception:  # noqa: BLE001
                log.exception("recently added check failed")
            try:
                self.mqtt.tick()
            except Exception:  # noqa: BLE001
                log.exception("MQTT publish failed")
            interval = settings.get(self.conn, "poll.interval_s")
            # A websocket nudge cuts the wait short, debounced by 1 s so a burst
            # of notifications becomes one poll.
            if self.nudge.wait(interval):
                self.nudge.clear()
                self.stop.wait(1)
                self.nudge.clear()
        self._teardown_ws()
        self.mqtt.close()
        self.notifier.stop()
        log.info("Worker stopped")


def main(data_dir: str | None = None) -> None:
    cfg = config_mod.load(data_dir)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.migrate_with_lock(cfg.db_path)
    worker = Worker(cfg)

    def _stop(*_a):
        worker.stop.set()
        worker.nudge.set()
        worker.job_wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    worker.run()

