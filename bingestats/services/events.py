"""The outside API's events (API standard section 8): what /api/v1/events streams and webhooks send.

Events start in the worker (sessions, arrivals, alerts, pins, downloads), so they go through the
`events` table rather than memory: the worker writes a row (and one webhook delivery per matching
webhook), and each web worker's stream reads rows after the last id it sent. The row id is the
SSE id and Last-Event-ID. Rows older than an hour are pruned, always keeping the last 200.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

KEEP_S = 3600
KEEP_MIN = 200
REPLAY_MAX = 200

# Every notification trigger, as a dotted event type. A test checks every trigger is here.
TRIGGER_EVENTS = {
    "play_start": "playback.start",
    "play_stop": "playback.stop",
    "pause": "playback.pause",
    "resume": "playback.resume",
    "buffer": "playback.buffering",
    "watched": "playback.watched",
    "transcode_start": "playback.transcode",
    "recently_added": "arrival",
    "server_down": "server.down",
    "server_up": "server.up",
    "stream_limit": "alert.raised",
    "transcode_limit": "alert.raised",
    "concurrent_ips": "alert.raised",
    "new_country": "alert.raised",
    "pin_arrived": "pin.arrived",
    "pin_schedule_changed": "pin.schedule_changed",
    "pins_week": "pin.week",
    "pins_discover": "pin.discover",
}
DOWNLOAD_FINISHED = "download.finished"
TYPES = list(dict.fromkeys([*TRIGGER_EVENTS.values(), DOWNLOAD_FINISHED]))

_VERBS = {"play_start": "started", "play_stop": "stopped", "pause": "paused", "resume": "resumed",
          "buffer": "keeps buffering on", "watched": "finished", "transcode_start": "is transcoding"}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _poster(rating_key) -> str | None:
    return f"/api/v1/images/poster/{rating_key}" if rating_key else None


def _clean(d: dict) -> dict:
    out = {k: v for k, v in d.items() if v is not None}
    if isinstance(out.get("meta"), dict):
        out["meta"] = {k: v for k, v in out["meta"].items() if v is not None}
    return out


def from_context(trigger: str, ctx: dict) -> tuple[str, dict]:
    """(title, item) for a notification context (notify.build_context), in the feeds' item shape."""
    group_of = {"play_start": "session", "pause": "session", "resume": "session", "buffer": "session",
                "watched": "session", "transcode_start": "session", "play_stop": "stop", "recently_added": "added",
                "server_down": "server", "server_up": "server"}
    group = group_of.get(trigger) or ("pins" if trigger.startswith("pin") else "limit")
    at = ctx.get("now") or int(time.time())
    if group in ("session", "stop", "limit"):
        user, item, stream = ctx.get("user") or {}, ctx.get("item") or {}, ctx.get("stream") or {}
        name, what = user.get("name") or "Someone", item.get("display_title") or "something"
        meta = {"user_id": user.get("id"), "user_name": name, "rating_key": item.get("rating_key"),
                "media_type": item.get("type"), "decision": stream.get("decision"),
                "player": (ctx.get("player") or {}).get("name"), "progress_pct": (ctx.get("progress") or {}).get("pct"),
                "state": ctx.get("state")}
        if group == "stop":
            play = ctx.get("play") or {}
            meta.update(play_id=play.get("id"), play_s=play.get("seconds"), watched=play.get("watched"))
        if group == "limit":
            meta["kind"] = trigger
            title = f"{ctx.get('trigger_label', 'Alert')}: {name}"
        else:
            title = f"{name} {_VERBS.get(trigger, trigger)} {what}"
        sub = " · ".join(x for x in (item.get("subtitle"), name, meta["player"]) if x)
        return title, _clean({"id": f"session:{ctx.get('_key')}", "title": what, "subtitle": sub or None, "at": iso(at),
                              "tone": "warning" if stream.get("decision") == "transcode" or group == "limit" else None,
                              "image": _poster(item.get("rating_key")), "meta": meta})
    if group == "added":
        headline = ctx.get("headline") or "Something new"
        return f"Just arrived: {headline}", _clean({
            "id": f"arrival:{ctx.get('_key')}", "title": ctx.get("show") or headline, "subtitle": headline, "at": iso(at),
            "tone": "accent", "meta": {"kind": ctx.get("kind"), "library_id": (ctx.get("library") or {}).get("id"),
                                       "count": ctx.get("count"), "season": ctx.get("season")}})
    if group == "server":
        down = trigger == "server_down"
        title = "Plex is down" if down else "Plex is back"
        sub = (ctx.get("error") or None) if down else f"Down for {round((ctx.get('down_for') or 0) / 60)} min"
        return title, _clean({"id": f"server:{at}", "title": title, "subtitle": sub, "at": iso(at),
                              "tone": "danger" if down else "success", "meta": {"down_for_s": ctx.get("down_for")}})
    # pins
    pinner = ctx.get("pinner") or {}
    headline = ctx.get("headline") or ctx.get("show") or ctx.get("trigger_label") or "Pins"
    if trigger == "pin_schedule_changed":
        headline = f"{ctx.get('show')} {ctx.get('code')}: now {ctx.get('new_text')}"
    return f"{ctx.get('trigger_label', 'Pins')}: {headline}", _clean({
        "id": f"pin:{ctx.get('_key')}", "title": ctx.get("show") or ctx.get("trigger_label") or "Pins",
        "subtitle": headline, "at": iso(at), "tone": "accent",
        "meta": {"user_id": pinner.get("id"), "user_name": pinner.get("name"), "count": ctx.get("count"),
                 "season": ctx.get("season"), "episode": ctx.get("episode")}})


def download_item(job: dict, recs: list[dict] | None, now: float) -> tuple[str, dict]:
    """A finished SABnzbd job (parse.sab_pp), described by its Sonarr/Radarr records if it has any."""
    r = (recs or [{}])[0]
    title = r.get("title") or job.get("name") or "A download"
    sub = None
    if r.get("app") == "sonarr":
        eps = sorted({(x.get("season"), x.get("episode")) for x in recs or [] if x.get("season") is not None})
        if len(eps) == 1 and eps[0][1] is not None:
            sub = f"S{eps[0][0]:02d}E{eps[0][1]:02d}"
        elif eps:
            sub = f"{len(eps)} episodes"
    elif r.get("year"):
        sub = str(r["year"])
    return f"Downloaded {title}" + (f" {sub}" if sub else ""), _clean({
        "id": f"sab:{job.get('nzo_id')}", "title": title, "subtitle": sub, "at": iso(job.get("completed_at") or now),
        "tone": "success", "meta": {"nzo_id": job.get("nzo_id"), "app": r.get("app"), "size_mb": job.get("mb"),
                                    "tvdb_id": r.get("tvdb_id"), "tmdb_id": r.get("tmdb_id")}})


# -- the table --------------------------------------------------------------------------------

def publish(conn: sqlite3.Connection, type_: str, title: str, item: dict, now: float | None = None) -> int:
    """Write an event, and a delivery for each enabled webhook that wants it. Worker only."""
    now = int(now or time.time())
    cur = conn.execute("INSERT INTO events (type, at, title, item) VALUES (?, ?, ?, ?)",
                       (type_, now, title, json.dumps(item, separators=(",", ":"))))
    event_id = cur.lastrowid
    from . import webhooks
    webhooks.enqueue(conn, event_id, type_, now)
    return event_id


def publish_trigger(conn: sqlite3.Connection, trigger: str, ctx: dict, now: float | None = None) -> int | None:
    type_ = TRIGGER_EVENTS.get(trigger)
    if type_ is None:
        return None
    title, item = from_context(trigger, ctx)
    return publish(conn, type_, title, item, now)


def data(row: sqlite3.Row | dict) -> dict:
    """What a stream's `data:` line and a webhook body carry."""
    return {"id": row["id"], "type": row["type"], "at": iso(row["at"]), "title": row["title"],
            "item": json.loads(row["item"])}


def after(conn: sqlite3.Connection, last_id: int, limit: int = REPLAY_MAX) -> list[dict]:
    return [data(r) for r in conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit))]


def last_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def get(conn: sqlite3.Connection, event_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return data(row) if row else None


def prune(conn: sqlite3.Connection, now: float | None = None) -> int:
    now = int(now or time.time())
    keep_from = last_id(conn) - KEEP_MIN
    return conn.execute("DELETE FROM events WHERE at < ? AND id <= ?", (now - KEEP_S, keep_from)).rowcount
