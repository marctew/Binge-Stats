"""Live download queue: SABnzbd's jobs, named by Sonarr and Radarr.

A thread in the worker. The whole queue (1,400+ jobs on a busy server, and
SABnzbd can take 40 s to list them while it's downloading flat out) is read
only now and then: at start, once a minute, when jobs are added and when the
category setting changes. That read decides the rows: the jobs to show, and
the runs of other jobs between them, grouped.

Every few seconds in between, only the jobs on show are asked for, by
nzo_id, which is instant. What SABnzbd has downloaded since the full read
that wasn't one of those jobs comes off the groups, front of the queue first,
and every row's ETA is worked out again from the MB ahead of it and the
current speed, the same sum SABnzbd does.

Sonarr's and Radarr's own queues say what each job is: their downloadId *is*
SABnzbd's nzo_id, so the match is exact. Those are only re-read when a new job
appears, or once a minute while busy. The last few history entries give jobs
being verified or unpacked.

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
from ..services import settings

log = logging.getLogger("bingestats.downloads")

BUSY_S, IDLE_S, ARR_REFRESH_S = 3, 30, 60
FULL_EVERY_S = 60          # the whole queue, at most this far apart
FULL_AFTER_ADD_S = 10      # sooner when jobs are added, but not on every tick of a burst
FULL_TIMEOUT_S = 90        # SABnzbd is slow to list a big queue while it's busy
MAX_ROWS = 400             # after grouping; a pathological queue still stays a sensible size

MOVE_CHECK_S = 0.5         # how often the wait between reads looks for a move made from the page


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
                 "downloading": False, "first_id": s["nzo_id"], "last_id": s["nzo_id"]}
            rows.append(g)
        g["last_id"] = s["nzo_id"]
        g["count"] += 1
        g["mb"] += s["mb"]
        g["mb_left"] += s["mb_left"]
        g["eta_s"] = s["eta_s"] if s["eta_s"] is not None else g["eta_s"]
        g["downloading"] = g["downloading"] or s["status"] == "Downloading" and s["pct"] > 0
    if len(rows) > MAX_ROWS:
        rest = rows[MAX_ROWS - 1:]
        tail = {"kind": "other", "count": sum(r.get("count", 1) for r in rest), "mb": sum(r["mb"] for r in rest),
                "mb_left": sum(r["mb_left"] for r in rest), "eta_s": rest[-1].get("eta_s"),
                "position": rest[0].get("position", rest[0].get("index")), "downloading": False,
                "first_id": rest[0].get("first_id", rest[0].get("nzo_id")),
                "last_id": rest[-1].get("last_id", rest[-1].get("nzo_id"))}
        rows = rows[:MAX_ROWS - 1] + [tail]
    return rows


def refresh(base: list[dict], header: dict, fresh: dict, base_mb_left: float) -> list[dict]:
    """The rows from the last full read, brought up to date. `fresh` is the jobs on show as
    SABnzbd has them now (by nzo_id); one that's gone has finished or been removed. Whatever
    else has been downloaded since comes off the groups, front first. ETAs are the MB ahead
    (this row included) over the current speed."""
    rows, job_done = [], 0.0
    for r in base:
        if r["kind"] != "job":
            rows.append(dict(r))
            continue
        s = fresh.get(r["nzo_id"])
        job_done += r["mb_left"] - (s["mb_left"] if s else 0.0)
        if s:
            rows.append({**r, **s, "kind": "job"})
    other_done = max(0.0, base_mb_left - header["mb_left"] - job_done)
    for g in rows:
        if other_done <= 0:
            break
        if g["kind"] == "other" and g["mb_left"] > 0:
            took = min(g["mb_left"], other_done)
            g["mb_left"] -= took
            other_done -= took
            g["downloading"] = True
    rows = [r for r in rows if r["kind"] == "job" or r["mb_left"] > 0.05]
    mbps = header["kbps"] / 1024
    ahead = 0.0
    for r in rows:
        ahead += r["mb_left"]
        if r["kind"] == "other" or r.get("status") != "Paused":
            r["eta_s"] = int(ahead / mbps) if mbps > 0 and not header["paused"] else None
    return rows


def _lookup(clients: dict, ids: set) -> dict:
    """What Sonarr and Radarr say each of these downloads is."""
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
                log.warning("%s queue: %s", app, e)
    return found


def snapshot(conn, clients: dict, st: dict, now: float) -> dict:
    """One read; `st` is the watcher's state between reads (see DownloadWatcher). Returns the
    snapshot for the page. Raises ArrError when SABnzbd can't be read at all."""
    sab = clients["sabnzbd"]
    cats = arr_svc.visible_categories(conn)
    hist = (sab.get("history", limit=20).get("history") or {}).get("slots") or []
    pp = [parse.sab_pp(h) for h in hist if h.get("status") in parse.PP_STATES]
    recent = [parse.sab_pp(h) for h in hist if h.get("status") in ("Completed", "Failed")
              and (int(h.get("completed") or 0) >= now - 6 * 3600)][:10]

    fresh = None
    moved = moved_since(conn, st)
    if moved:
        st["moved_seen"] = moved
        st["full_at"] = 0.0                                 # a job was moved from the page: read everything now
    if st.get("base") is not None and cats == st.get("cats") and now - st["full_at"] < FULL_EVERY_S:
        ids = [r["nzo_id"] for r in st["base"] if r["kind"] == "job"]
        q = sab.get("queue", limit=0 if ids else 1, **({"nzo_ids": ",".join(ids)} if ids else {})).get("queue") or {}
        header = parse.sab_queue(q)
        if header["total"] > st["total"] and now - st["full_at"] >= FULL_AFTER_ADD_S:
            fresh = None                                    # jobs were added: time for a full read
        else:
            wanted = set(ids)
            fresh = {s["nzo_id"]: s for s in map(parse.sab_slot, q.get("slots") or []) if s["nzo_id"] in wanted}
    if fresh is None:
        try:
            q = sab.get("queue", limit=0, timeout=FULL_TIMEOUT_S).get("queue") or {}
        except ArrError:
            if st.get("base") is not None:
                st["full_at"] = now                         # keep the rows we have; try again in a minute
            raise
        header = parse.sab_queue(q)
        slots = [parse.sab_slot(s) for s in q.get("slots") or []]
        ids = {s["nzo_id"] for s in slots} | {p["nzo_id"] for p in pp} | {r["nzo_id"] for r in recent}
        known = st.get("known", {})
        if ids and ((ids - st.get("checked", set())) or now - st.get("arr_read_at", 0.0) >= ARR_REFRESH_S):
            known = {**known, **_lookup(clients, ids)}
            st["arr_read_at"] = now
        st["known"] = {k: v for k, v in known.items() if k in ids}
        st["checked"] = ids
        show = lambda s: cats is None or (s.get("cat") or "").lower() in cats or s.get("nzo_id") in st["known"]  # noqa: E731
        st.update(base=group_rows(slots, show), base_mb_left=header["mb_left"], total=header["total"],
                  full_at=now, cats=cats)
        fresh = {s["nzo_id"]: s for s in slots}
    elif {p["nzo_id"] for p in pp} - st.get("checked", set()):
        st["known"] = {**st.get("known", {}), **_lookup(clients, {p["nzo_id"] for p in pp})}
        st["checked"] = st.get("checked", set()) | {p["nzo_id"] for p in pp}

    rows = refresh(st["base"], header, fresh, st["base_mb_left"])
    return {"header": header, "rows": rows, "pp": pp, "recent": recent, "at": int(now), "full_at": round(st["full_at"], 3)}


def moved_since(conn, st: dict) -> float:
    """When a job was last moved from the Downloads page, if it's since the last full read."""
    moved = float(settings.get(conn, "sabnzbd.moved_at") or 0)
    return moved if moved > st.get("moved_seen", 0.0) else 0.0


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
        # known: nzo_id -> Sonarr/Radarr records; checked: ids already looked up; base: rows from the
        # last full read, with base_mb_left and total (SABnzbd's MB left and job count then).
        self.st: dict = {"known": {}, "checked": set(), "arr_read_at": 0.0, "base": None, "full_at": 0.0}
        self.failing = False

    @property
    def known(self) -> dict:
        return self.st["known"]

    def tick(self, conn, now: float | None = None) -> bool:
        """One read. Returns True while SABnzbd has work, so the next read comes sooner."""
        clients = arr_svc.clients(conn)
        if "sabnzbd" not in clients:
            return False
        now = now or time.time()
        try:
            live = snapshot(conn, clients, self.st, now)
        except ArrError as e:
            if not self.failing:
                log.warning("SABnzbd queue: %s (keeping the last snapshot)", e)
            self.failing = True
            return self.st.get("base") is not None
        if self.failing:
            log.info("SABnzbd queue: reading again")
        self.failing = False
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
            deadline = time.monotonic() + (BUSY_S if busy else IDLE_S)
            while not self.stop_event.wait(MOVE_CHECK_S) and time.monotonic() < deadline:
                if moved_since(conn, self.st):
                    break                                # a move: read again now, not in up to 3 s
        conn.close()
