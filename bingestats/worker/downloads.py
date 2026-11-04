"""Live download queue: SABnzbd's jobs, named by Sonarr and Radarr.

A thread in the worker. While SABnzbd is busy it reads the queue every few
seconds (every 30 s when idle) and the last few history entries, for jobs
being verified or unpacked. Sonarr's and Radarr's own queues say what each
job is: their downloadId *is* SABnzbd's nzo_id, so the match is exact. Those
are only re-read when a new job appears, or once a minute while busy.

The snapshot goes into arr_status (app 'sabnzbd', info.live) for the
Downloads page; nothing is kept once a job leaves the queue.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from .. import db
from ..arr import parse
from ..arr.client import ArrError
from ..services import arr as arr_svc

log = logging.getLogger("bingestats.downloads")

BUSY_S, IDLE_S, ARR_REFRESH_S = 3, 30, 60
MAX_ROWS = 400             # after grouping; a pathological queue still stays a sensible size


def group_rows(slots: list[dict], show) -> list[dict]:
    """Jobs to show stay as rows; each run of other jobs between them becomes one
    'other' row with its count, size and when the last of them finishes."""
    rows: list[dict] = []
    for s in slots:
        if show(s):
            rows.append({"kind": "job", **s})
            continue
        if rows and rows[-1]["kind"] == "other":
            g = rows[-1]
        else:
            g = {"kind": "other", "count": 0, "mb": 0.0, "mb_left": 0.0, "eta_s": None, "position": s.get("index"),
                 "downloading": False}
            rows.append(g)
        g["count"] += 1
        g["mb"] += s["mb"]
        g["mb_left"] += s["mb_left"]
        g["eta_s"] = s["eta_s"] if s["eta_s"] is not None else g["eta_s"]
        g["downloading"] = g["downloading"] or s["status"] == "Downloading" and s["pct"] > 0
    if len(rows) > MAX_ROWS:
        rest = rows[MAX_ROWS - 1:]
        tail = {"kind": "other", "count": sum(r.get("count", 1) for r in rest), "mb": sum(r["mb"] for r in rest),
                "mb_left": sum(r["mb_left"] for r in rest), "eta_s": rest[-1].get("eta_s"),
                "position": rest[0].get("position", rest[0].get("index")), "downloading": False}
        rows = rows[:MAX_ROWS - 1] + [tail]
    return rows


def snapshot(conn, clients: dict, known: dict, checked: set, now: float, arr_read_at: float):
    """Read SAB (and Sonarr/Radarr when a job appears that hasn't been looked up yet, or once a
    minute while busy). Returns (live, known map, checked ids, when Sonarr/Radarr were last read).
    Jobs neither app knows about stay 'checked', so they don't cause a lookup every few seconds."""
    sab = clients["sabnzbd"]
    q = sab.get("queue", limit=0).get("queue") or {}       # the whole queue: ~0.6 MB for 1,400 jobs
    header = parse.sab_queue(q)
    slots = [parse.sab_slot(s) for s in q.get("slots") or []]
    hist = (sab.get("history", limit=20).get("history") or {}).get("slots") or []
    pp = [parse.sab_pp(h) for h in hist if h.get("status") in parse.PP_STATES]
    recent = [parse.sab_pp(h) for h in hist if h.get("status") in ("Completed", "Failed")
              and (int(h.get("completed") or 0) >= now - 6 * 3600)][:10]
    ids = {s["nzo_id"] for s in slots} | {p["nzo_id"] for p in pp} | {r["nzo_id"] for r in recent}
    if ids and ((ids - checked) or now - arr_read_at >= ARR_REFRESH_S):
        found: dict[str, list] = {}
        for app, inc in (("sonarr", {"includeSeries": "true", "includeEpisode": "true"}),
                         ("radarr", {"includeMovie": "true"})):
            if app in clients:
                try:
                    for r in clients[app].paged("queue", page_size=500, max_pages=4, **inc):
                        rec = parse.arr_queue_record(r, app)
                        if rec["download_id"]:
                            found.setdefault(rec["download_id"], []).append(rec)
                except ArrError as e:
                    log.debug("%s queue: %s", app, e)
        known = {k: v for k, v in {**known, **found}.items() if k in ids}
        arr_read_at = now
    else:
        known = {k: v for k, v in known.items() if k in ids}
    cats = arr_svc.visible_categories(conn)
    show = lambda s: cats is None or (s.get("cat") or "").lower() in cats or s.get("nzo_id") in known  # noqa: E731
    live = {"header": header, "rows": group_rows(slots, show), "pp": pp, "recent": recent, "at": int(now)}
    return live, known, ids, arr_read_at


def store(conn, live: dict, known: dict) -> None:
    with db.transaction(conn):
        row = conn.execute("SELECT info FROM arr_status WHERE app = 'sabnzbd'").fetchone()
        info = json.loads(row["info"]) if row else {}
        info.update(queue=live["header"], queue_at=live["at"], live=live, live_map=known)
        conn.execute("INSERT INTO arr_status (app, ok, info) VALUES ('sabnzbd', 1, ?) "
                     "ON CONFLICT(app) DO UPDATE SET info = excluded.info", (json.dumps(info),))


class DownloadWatcher(threading.Thread):
    def __init__(self, db_path: Path, stop: threading.Event):
        super().__init__(name="downloads", daemon=True)
        self.db_path, self.stop_event = db_path, stop
        self.known: dict = {}
        self.checked: set = set()
        self.arr_read_at = 0.0

    def tick(self, conn, now: float | None = None) -> bool:
        """One read. Returns True while SABnzbd has work, so the next read comes sooner."""
        clients = arr_svc.clients(conn)
        if "sabnzbd" not in clients:
            return False
        now = now or time.time()
        try:
            live, self.known, self.checked, self.arr_read_at = snapshot(conn, clients, self.known, self.checked, now,
                                                                         self.arr_read_at)
        except ArrError as e:
            log.debug("SABnzbd queue: %s", e)
            return False
        store(conn, live, self.known)
        return bool(live["rows"] or live["pp"])

    def run(self) -> None:
        conn = db.connect(self.db_path)
        while not self.stop_event.is_set():
            busy = False
            try:
                busy = self.tick(conn)
            except Exception:  # noqa: BLE001 - never take the worker down over a status page
                log.exception("download watcher failed")
            self.stop_event.wait(BUSY_S if busy else IDLE_S)
        conn.close()
