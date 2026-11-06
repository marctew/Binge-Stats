"""The outside API's answers in the standard's shapes (docs/specs/api-standard.md).

Everything here reads the database only: it never calls Plex or the *arrs in a request, so a
display polling every few seconds costs a few queries, and status still answers during an import.
Times come in as epoch seconds and leave as ISO 8601 UTC (`...Z`), converted only here.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable
from datetime import datetime, timezone

from . import activity, alerts, pin_api, pin_lists, settings

STATUS_REFRESH_S = 15
APP_NAMES = {"sonarr": "Sonarr", "radarr": "Radarr", "sabnzbd": "SABnzbd", "tmdb": "TMDB"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _downloading(conn: sqlite3.Connection) -> int:
    """TV and film jobs in SABnzbd's queue, from the watcher's last read."""
    row = conn.execute("SELECT info FROM arr_status WHERE app = 'sabnzbd'").fetchone()
    info = json.loads(row["info"]) if row and row["info"] else {}
    return sum(1 for r in (info.get("live") or {}).get("rows") or [] if r.get("kind") == "job")


def _pins_ready(conn: sqlite3.Connection, now: float) -> int:
    uid = pin_api.resolve_user(conn, None)
    if uid is None:
        return 0
    return sum(r["waiting"] for r in pin_lists.ready(conn, uid, now))


def _stream_line(a) -> str:
    s = a.session
    what = s.grandparent_title or s.title or "Something"
    if s.media_type == "episode" and s.season_index is not None and s.episode_index is not None:
        what += f" S{int(s.season_index):02d}E{int(s.episode_index):02d}"
    elif s.media_type == "movie" and s.year:
        what += f" ({s.year})"
    return f"{a.user_name} · {what}, {round(a.progress_pct)}%"


def status(conn: sqlite3.Connection, url: str, now: float | None = None) -> dict:
    now = now or time.time()
    worker = activity.worker_status(conn, now)
    srv = conn.execute("SELECT is_up FROM servers ORDER BY id LIMIT 1").fetchone()
    active = activity.list_active(conn)
    totals = activity.totals(active)
    n_alerts = alerts.unacked_count(conn)
    failing = [APP_NAMES.get(r[0], r[0]) for r in conn.execute("SELECT app FROM arr_status WHERE ok = 0 ORDER BY app")]
    busy = [r[0] for r in conn.execute("SELECT DISTINCT kind FROM jobs WHERE status = 'running'")]

    streams, transcodes = totals["streams"], totals.get("transcode", 0)
    if streams:
        playing = _plural(streams, "stream") + (f", {transcodes} transcoding" if transcodes else "")
    else:
        playing = "Nothing playing"
    detail = _stream_line(active[0]) if active else None

    if not worker["plex_configured"]:
        state, headline = "error", "Plex isn't set up yet"
    elif srv is not None and not srv["is_up"]:
        state, headline = "error", "Plex is down"
    elif not worker["alive"]:
        state, headline = "error", "The worker isn't running"
    elif n_alerts or failing:
        state, headline = "warning", playing
        reasons = []
        if n_alerts:
            reasons.append(_plural(n_alerts, "alert") + " to look at")
        if failing:
            reasons.append(f"{_and(failing)} didn't answer last time")
        detail = "; ".join(reasons)
    elif busy:
        state, headline = "busy", playing
        detail = detail or ("Running: " + ", ".join(k.replace("_", " ") for k in busy))
    else:
        state, headline = "ok", playing

    counts = [
        {"key": "streams", "label": "Streams", "value": streams},
        {"key": "transcodes", "label": "Transcoding", "value": transcodes},
        {"key": "bandwidth_mbps", "label": "Bandwidth (Mbps)", "value": round(totals["bandwidth_kbps"] / 1000, 1)},
        {"key": "alerts", "label": "Alerts", "value": n_alerts, **({"tone": "warning"} if n_alerts else {})},
        {"key": "downloads", "label": "Downloading", "value": _downloading(conn)},
        {"key": "pins_ready", "label": "Ready to watch", "value": _pins_ready(conn, now)},
    ]
    out = {"app": "binge-stats", "state": state, "headline": headline}
    if detail:
        out["detail"] = detail
    out.update(counts=counts, url=url, generated_at=_iso(now), refresh_s=STATUS_REFRESH_S)
    return out


def public_url(conn: sqlite3.Connection, fallback: str) -> str:
    return (settings.get(conn, "ui.public_url") or fallback).rstrip("/") + "/"


# -- feeds (API standard section 5) ----------------------------------------------------------------

DEFAULT_LIMIT, MAX_LIMIT = 20, 100
GROUP_WINDOW_S = 6 * 3600     # episodes of one season added this close together are one arrival


def at_label(ts: float | None, now: float, tz) -> str | None:
    """The moment written for a person in ui.timezone, so a panel or a Liquid template needn't do
    time zones: 'Now', 'in 2 h', 'Today 19:12', 'Yesterday 19:12', 'Sat 7 Nov', '7 Nov 2025'."""
    if ts is None:
        return None
    diff = ts - now
    if abs(diff) < 60:
        return "Now"
    if 0 < diff < 12 * 3600:
        return f"in {round(diff / 60)} min" if diff < 3600 else f"in {round(diff / 3600)} h"
    t, today = datetime.fromtimestamp(ts, tz), datetime.fromtimestamp(now, tz).date()
    days = (t.date() - today).days
    if days == 0:
        return f"Today {t:%H:%M}"
    if days == -1:
        return f"Yesterday {t:%H:%M}"
    if days == 1:
        return f"Tomorrow {t:%H:%M}"
    if -6 <= days <= 6:
        return f"{t:%a} {t.day} {t:%b}"
    return f"{t.day} {t:%b %Y}"


@dataclass
class FeedArgs:
    limit: int
    since: float | None        # items with `at` after this
    cursor: str | None
    args: dict                 # the feed's own params, as given
    now: float
    tz: Any
    base: str                  # the app's public URL, ending in /

    def int_arg(self, name: str, default: int | None = None, lo: int | None = None, hi: int | None = None):
        try:
            n = int(self.args.get(name)) if self.args.get(name) not in (None, "") else default
        except (TypeError, ValueError):
            n = default
        if n is not None and lo is not None:
            n = max(lo, n)
        if n is not None and hi is not None:
            n = min(hi, n)
        return n


@dataclass
class Feed:
    name: str
    title: str
    refresh_s: int
    params: dict
    build: Callable[[sqlite3.Connection, FeedArgs], tuple[list[dict], str | None]]
    timed: bool = True         # ordered by time, so `since` and `cursor` apply


FEEDS: dict[str, Feed] = {}


def _feed(name: str, title: str, refresh_s: int, params: dict | None = None, timed: bool = True):
    def deco(fn):
        FEEDS[name] = Feed(name, title, refresh_s, params or {}, fn, timed)
        return fn
    return deco


def feed_args(conn: sqlite3.Connection, base: str, *, limit: int = DEFAULT_LIMIT, since: float | None = None,
              cursor: str | None = None, args: dict | None = None, now: float | None = None) -> FeedArgs:
    from . import stats
    return FeedArgs(limit=max(1, min(MAX_LIMIT, limit)), since=since, cursor=cursor, args=dict(args or {}),
                    now=now or time.time(), tz=stats._tz(conn), base=base)


def feed(conn: sqlite3.Connection, name: str, fa: FeedArgs) -> dict:
    f = FEEDS[name]
    items, cursor = f.build(conn, fa)
    for it in items:
        it["at_label"] = at_label(it["_at"], fa.now, fa.tz) if it.get("_at") is not None else None
        it["at"] = _iso(it["_at"]) if it.get("_at") is not None else None
        it.pop("_at", None)
        if isinstance(it.get("meta"), dict):
            it["meta"] = {k: v for k, v in it["meta"].items() if v is not None}
        for k in [k for k, v in it.items() if v is None]:
            del it[k]
    return {"feed": f.name, "title": f.title, "items": items, "next_cursor": cursor,
            "generated_at": _iso(fa.now), "refresh_s": f.refresh_s}


def _cursor_at(cursor: str | None) -> tuple[float, str] | None:
    """Cursors are '<epoch>:<id>' of the last item on the page."""
    if not cursor or ":" not in cursor:
        return None
    at, _, ident = cursor.partition(":")
    try:
        return float(at), ident
    except ValueError:
        return None


def _tiebreak(ident: str) -> tuple:
    """'plex:58213' sorts by the number, so 'plex:9' comes before 'plex:10'."""
    head, _, tail = ident.rpartition(":")
    return (head, int(tail), "") if tail.isdigit() else (head, -1, tail)


def _order(item: dict) -> tuple:
    return (int(item["_at"] or 0), _tiebreak(item["id"]))


def _page(items: list[dict], fa: FeedArgs) -> tuple[list[dict], str | None]:
    """since, cursor and limit over items already newest first (by `_order`), each with `_at` and `id`."""
    if fa.since is not None:
        items = [i for i in items if i["_at"] is not None and i["_at"] > fa.since]
    c = _cursor_at(fa.cursor)
    if c is not None:
        mark = (int(c[0]), _tiebreak(c[1]))
        items = [i for i in items if _order(i) < mark]
    page = items[:fa.limit]
    more = len(items) > fa.limit
    return page, (f"{int(page[-1]['_at'] or 0)}:{page[-1]['id']}" if more and page else None)


def _code(season, episode) -> str:
    return f"S{int(season):02d}E{int(episode):02d}" if season is not None and episode is not None else ""


def _poster(rating_key) -> str | None:
    return f"/api/v1/images/poster/{rating_key}" if rating_key else None


def _session_title(s) -> tuple[str, str]:
    if s.media_type == "episode":
        sub = " · ".join(x for x in (_code(s.season_index, s.episode_index), s.title) if x)
        return s.grandparent_title or s.title or "Unknown", sub
    if s.media_type == "track":
        return s.title or "Unknown", " · ".join(x for x in (s.grandparent_title, s.parent_title) if x)
    return s.title or "Unknown", str(s.year or "")


@_feed("now_playing", "Now playing", 10, timed=False)
def _now_playing(conn, fa):
    out = []
    for a in activity.list_active(conn):
        s = a.session
        title, sub = _session_title(s)
        art_key = s.grandparent_rating_key if s.media_type == "episode" else (
            s.parent_rating_key if s.media_type == "track" else s.rating_key)
        out.append({
            "id": f"session:{s.session_id}", "title": title,
            "subtitle": " · ".join(x for x in (sub, a.user_name, s.player_title or s.product) if x),
            "_at": a.started_at, "tone": "warning" if s.stream.decision == "transcode" else None,
            "image": _poster(art_key), "url": fa.base + "activity",
            "meta": {"user_id": a.user_id, "user_name": a.user_name, "progress_pct": round(a.progress_pct, 1),
                     "state": a.state, "decision": s.stream.decision, "bandwidth_kbps": s.bandwidth_kbps,
                     "player": s.player_title or s.product, "rating_key": s.rating_key,
                     "media_type": s.media_type}})
    return out[:fa.limit], None


@_feed("arrivals", "Just arrived", 300, {"kind": "movie | show | album"})
def _arrivals(conn, fa):
    kind = fa.args.get("kind")
    types = {"movie": ("movie",), "show": ("episode",), "album": ("album",)}.get(kind, ("movie", "episode", "album"))
    rows = conn.execute(
        f"""SELECT mi.rating_key, mi.type, mi.title, mi.parent_title, mi.grandparent_title, mi.grandparent_rating_key,
                   mi.season_index, mi.episode_index, mi.year, mi.added_at, mi.library_id
            FROM media_items mi WHERE mi.is_present = 1 AND mi.added_at IS NOT NULL
              AND mi.type IN ({','.join('?' * len(types))}) {'AND mi.added_at > ?' if fa.since is not None else ''}
            ORDER BY mi.added_at DESC, mi.rating_key DESC LIMIT 2000""",
        [*types, *([int(fa.since)] if fa.since is not None else [])]).fetchall()
    groups: list[dict] = []
    open_eps: dict[tuple, dict] = {}       # (show, season) -> the arrival it's joining, newest first
    for r in rows:
        if r["type"] == "episode":
            k = (r["grandparent_rating_key"], r["season_index"])
            g = open_eps.get(k)
            if g is not None and g["_oldest"] - r["added_at"] <= GROUP_WINDOW_S:
                g["_eps"].append(r)
                g["_oldest"] = r["added_at"]
                continue
            g = {"_eps": [r], "_oldest": r["added_at"], "_row": r}
            open_eps[k] = g
            groups.append(g)
        else:
            groups.append({"_eps": None, "_row": r})
    items = []
    for g in groups:
        r = g["_row"]
        if g["_eps"] is not None:
            eps = sorted(g["_eps"], key=lambda e: (e["episode_index"] or 0))
            first = eps[0]
            if len(eps) == 1:
                sub = " · ".join(x for x in (_code(first["season_index"], first["episode_index"]), first["title"]) if x)
            else:
                sub = (f"Season {first['season_index']} · " if first["season_index"] is not None else "") + \
                      f"{len(eps)} new episodes"
            items.append({"id": f"plex:{min(e['rating_key'] for e in eps)}", "title": r["grandparent_title"] or "A show",
                          "subtitle": sub, "_at": r["added_at"], "image": _poster(r["grandparent_rating_key"]),
                          "url": fa.base + f"item/{r['grandparent_rating_key']}",
                          "meta": {"kind": "show", "rating_key": first["rating_key"], "library_id": r["library_id"],
                                   "show_id": r["grandparent_rating_key"], "show_title": r["grandparent_title"],
                                   "season": first["season_index"], "episode": first["episode_index"],
                                   "episodes": len(eps)}})
        elif r["type"] == "movie":
            items.append({"id": f"plex:{r['rating_key']}", "title": r["title"], "subtitle": str(r["year"] or "") or None,
                          "_at": r["added_at"], "image": _poster(r["rating_key"]), "url": fa.base + f"item/{r['rating_key']}",
                          "meta": {"kind": "movie", "rating_key": r["rating_key"], "library_id": r["library_id"],
                                   "year": r["year"]}})
        else:
            items.append({"id": f"plex:{r['rating_key']}", "title": r["title"], "subtitle": r["parent_title"],
                          "_at": r["added_at"], "image": _poster(r["rating_key"]), "url": fa.base + f"item/{r['rating_key']}",
                          "meta": {"kind": "album", "rating_key": r["rating_key"], "library_id": r["library_id"],
                                   "artist": r["parent_title"], "year": r["year"]}})
    items.sort(key=_order, reverse=True)
    return _page(items, fa)


@_feed("history", "Recently watched", 60, {"user_id": "int", "kind": "movie | episode | track"})
def _history(conn, fa):
    from . import history
    hargs = {"user": fa.args.get("user_id"), "type": fa.args.get("kind")}
    c = _cursor_at(fa.cursor)
    if c is not None:
        hargs["before"] = f"{int(c[0])}:{c[1].removeprefix('play:')}"
    rows, cursor = history.list_history(conn, history.HistoryFilter.from_args(hargs),
                                        settings.get(conn, "ui.timezone"), limit=fa.limit)
    items = []
    for r in rows:
        p = r.play
        if fa.since is not None and (p["stopped_at"] or 0) <= fa.since:
            cursor = None
            break
        if p["media_type"] == "episode":
            title = p["grandparent_title"] or p["title"] or "Unknown"
            sub = " · ".join(x for x in (_code(p["season_index"], p["episode_index"]), p["title"]) if x)
            art = p.get("grandparent_rating_key")
        else:
            title, sub = p["title"] or "Unknown", (str(p["year"]) if p.get("year") else p.get("grandparent_title"))
            art = p.get("parent_rating_key") if p["media_type"] == "track" else p.get("rating_key")
        items.append({"id": f"play:{p['id']}", "title": title,
                      "subtitle": " · ".join(x for x in (sub, r.user_name) if x), "_at": p["stopped_at"],
                      "image": _poster(art), "url": fa.base + f"history?user={p['user_id']}",
                      "meta": {"user_id": p["user_id"], "user_name": r.user_name, "rating_key": p.get("rating_key"),
                               "media_type": p["media_type"], "play_s": r.total_play_s, "decision": p.get("decision"),
                               "player": p.get("player_title") or p.get("product"), "watched": bool(p.get("watched"))}})
    # history pages by started_at; translate its cursor to this feed's '<epoch>:play:<id>' form
    if cursor:
        started, _, pid = cursor.partition(":")
        cursor = f"{started}:play:{pid}"
    return items, cursor


@_feed("alerts", "Alerts", 60, {"all": "1 to include acknowledged"})
def _alerts(conn, fa):
    rows = alerts.list_alerts(conn, include_acked=fa.args.get("all") in ("1", "true"), limit=500)
    items = [{"id": f"alert:{a['id']}", "title": a["label"], "subtitle": a["text"], "_at": a["created_at"],
              "tone": {"ok": "success", "warn": "warning"}.get(a["severity"], "danger"), "url": fa.base + "alerts",
              "meta": {"alert_id": a["id"], "kind": a["kind"], "user_id": a["user_id"],
                       "acknowledged": bool(a["acknowledged"])}} for a in rows]
    return _page(items, fa)


@_feed("downloads", "Downloading", 30, timed=False)
def _downloads(conn, fa):
    from . import arr
    q = arr.download_queue(conn, fa.now)
    if not q or not q.get("ready"):
        return [], None
    out = []
    for j in q["items"][:fa.limit]:
        bits = [j.get("sub")]
        if j.get("eta_s") is not None:
            bits.append(f"{j['pct']}%" + (", stalled" if j.get("stalled_s") else ""))
        out.append({"id": f"sab:{j['nzo_id']}", "title": j["title"], "subtitle": " · ".join(b for b in bits if b) or None,
                    "_at": (fa.now + j["eta_s"]) if j.get("eta_s") is not None else None,
                    "tone": "warning" if j.get("stalled_s") else ("danger" if j.get("warning") else None),
                    "image": _poster(j.get("rating_key")), "url": fa.base + "downloads",
                    "meta": {"nzo_id": j["nzo_id"], "pct": j.get("pct"), "eta_s": j.get("eta_s"),
                             "stalled": bool(j.get("stalled_s")), "size_mb": round(j.get("mb") or 0),
                             "category": j.get("cat"), "app": j.get("app"), "pinned": j.get("pinned"),
                             "rating_key": j.get("rating_key")}})
    return out, None


def _pin_user(conn, fa) -> int | None:
    return pin_api.resolve_user(conn, fa.int_arg("user_id"))


def _show_image(tvdb_id, show_rating_key) -> str | None:
    if show_rating_key:
        return _poster(show_rating_key)
    return f"/api/v1/images/show/{tvdb_id}" if tvdb_id else None


@_feed("pins_schedule", "Pinned episodes", 300, {"user_id": "int", "days": "0-60, default 7", "back": "0-30, default 1"},
       timed=False)
def _pins_schedule(conn, fa):
    uid = _pin_user(conn, fa)
    if uid is None:
        return [], None
    eps = pin_api.schedule(conn, uid, days=fa.int_arg("days", 7, 0, 60), back=fa.int_arg("back", 1, 0, 30), now=fa.now)
    tones = {"available": "success", "on_disk": "success", "downloading": "accent", "airing_today": "accent",
             "missing": "danger"}
    items = [{"id": f"episode:{e['tvdb_id']}:{e['code'] or e['air_at']}", "title": e["show"],
              "subtitle": " · ".join(x for x in (e["code"], e.get("title"), e["label"]) if x), "_at": e["air_at"],
              "tone": "warning" if e["stalled"] else tones.get(e["state"]),
              "image": _show_image(e["tvdb_id"], e.get("show_rating_key")),
              "url": fa.base + "calendar",
              "meta": {"tvdb_id": e["tvdb_id"], "code": e["code"], "season": e["season"], "episode": e["episode"],
                       "state": e["state"], "downloading_pct": e["downloading_pct"], "stalled": e["stalled"],
                       "rating_key": e.get("rating_key")}} for e in eps]
    return items[:fa.limit], None


@_feed("pins_ready", "Ready to watch", 300, {"user_id": "int"}, timed=False)
def _feed_pins_ready(conn, fa):
    uid = _pin_user(conn, fa)
    if uid is None:
        return [], None
    items = [{"id": f"show:{r['tvdb_id']}", "title": r["show"],
              "subtitle": " · ".join(x for x in (r["next_code"], r.get("next_title"),
                                                 _plural(r["waiting"], "episode") + " waiting") if x),
              "_at": r["newest_at"] or None, "tone": "accent" if r["new"] else None,
              "image": _show_image(r["tvdb_id"], r.get("show_rating_key")),
              "url": fa.base + (f"item/{r['next_rating_key']}" if r.get("next_rating_key") else "calendar"),
              "meta": {"tvdb_id": r["tvdb_id"], "next": r["next_code"], "next_rating_key": r.get("next_rating_key"),
                       "waiting": r["waiting"], "new": r["new"]}}
             for r in pin_lists.ready(conn, uid, fa.now)]
    return items[:fa.limit], None
