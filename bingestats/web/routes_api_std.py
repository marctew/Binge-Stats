"""The API standard's routes (Design Uniformity API-STANDARD.md), beside Binge's own /api/v1 routes.

Registered on the same `api` blueprint, so the /api/v1 prefix and the login gate's exemption apply.
These answer in the standard's shapes and error shape; routes_api.py keeps Binge's older ones.
Thin: the answers are built in services/api_feeds.py.
"""
from __future__ import annotations

import time

from flask import current_app, jsonify, request

from ..services import activity, api_feeds, arr
from .. import du_api
from ..du_api import standard
from .routes_api import bp
from .routes_img import PLEX_PATH


def _db():
    return current_app.get_db()


@bp.route("/meta")
@standard("read")
def std_meta():
    return jsonify(du_api.core().meta())


@bp.route("/status")
@standard("read")
def std_status():
    conn = _db()
    body = api_feeds.status(conn, api_feeds.public_url(conn, request.host_url))
    return du_api.cached(jsonify(body), body["refresh_s"])


# -- feeds -------------------------------------------------------------------------------------

def register_feeds(core) -> None:
    for f in api_feeds.FEEDS.values():
        core.add_feed(f.name, f.title, f.refresh_s, f.params)


@bp.route("/feeds/<name>")
@standard("read")
def std_feed(name: str):
    if name not in api_feeds.FEEDS:
        names = ", ".join(sorted(api_feeds.FEEDS))
        return du_api.ApiCore.error("not_found", f"There's no feed called '{name}'. Feeds: {names}.", 404)
    try:
        limit = int(request.args.get("limit") or api_feeds.DEFAULT_LIMIT)
    except ValueError:
        return du_api.ApiCore.error("bad_request", "limit must be a whole number.", 400)
    since = None
    if request.args.get("since"):
        since = du_api.parse_iso(request.args["since"])
        if since is None:
            return du_api.ApiCore.error("bad_request", "since must be a time like 2026-11-05T19:00:00Z.", 400)
    conn = _db()
    args = {k: v for k, v in request.args.items() if k not in ("token", "limit", "since", "cursor")}
    fa = api_feeds.feed_args(conn, api_feeds.public_url(conn, request.host_url), limit=limit, since=since,
                             cursor=request.args.get("cursor") or None, args=args, now=time.time())
    body = api_feeds.feed(conn, name, fa)
    return du_api.cached(jsonify(body), body["refresh_s"])


# -- images ------------------------------------------------------------------------------------

def _plex_fetch(path: str | None, shape: float):
    """The original from Plex's photo transcoder, at least `box` wide. The token stays here."""
    def fetch(box: int) -> bytes | None:
        if not path or not PLEX_PATH.match(path):
            return None
        client = activity.plex_client(_db())
        if client is None:
            return None
        r = client.photo(path, box, int(box * shape))
        return r.content if r.headers.get("Content-Type", "").startswith("image/") else None
    return fetch


def _sonarr_fetch(series_id: int):
    def fetch(_box: int) -> bytes | None:
        client = arr.clients(_db()).get("sonarr")
        if client is None:
            return None
        r = client.http.get(f"{client.url}/api/v3/mediacover/{series_id}/poster-500.jpg",
                            headers={"X-Api-Key": client.api_key}, timeout=10)
        ok = r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/")
        return r.content if ok else None
    return fetch


def _artwork(rating_key: int, field: str) -> str | None:
    """A library item's poster (thumb) or art; an episode or season uses its show's, a track its album's."""
    conn = _db()
    row = conn.execute("SELECT type, thumb, art, parent_rating_key, grandparent_rating_key FROM media_items "
                       "WHERE rating_key = ?", (rating_key,)).fetchone()
    if row is None:
        return None
    up = {"episode": row["grandparent_rating_key"], "season": row["parent_rating_key"],
          "track": row["parent_rating_key"]}.get(row["type"])
    if up:
        parent = conn.execute(f"SELECT {field} FROM media_items WHERE rating_key = ?", (up,)).fetchone()
        if parent is not None and parent[field]:
            return parent[field]
    return row[field]


@bp.route("/images/<kind>/<int:ident>")
@standard("read")
def std_image(kind: str, ident: int):
    cache = current_app.config["BS_CONFIG"].data_dir / "image-cache"
    if kind in ("poster", "art"):
        path = _artwork(ident, "thumb" if kind == "poster" else "art")
        fetch = _plex_fetch(path, 1.5 if kind == "poster" else 9 / 16)
        return du_api.image_response(cache, kind, f"{ident}:{path}", fetch)
    if kind == "show":
        from ..services import pin_api
        src = pin_api.poster_source(_db(), ident)
        if src is None:
            return du_api.ApiCore.error("not_found", "There's no image for that.", 404)
        fetch = _plex_fetch(src[1], 1.5) if src[0] == "plex" else _sonarr_fetch(int(src[1]))
        return du_api.image_response(cache, kind, f"{ident}:{src[0]}:{src[1]}", fetch)
    return du_api.ApiCore.error("not_found", "Image kinds: poster, art, show.", 404)


# -- actions -----------------------------------------------------------------------------------

def register_actions(core, get_db) -> None:
    from ..services import api_actions
    for a in api_actions.ACTIONS.values():
        when = (lambda a=a: a.when(get_db())) if a.when else None
        core.add_action(a.name, a.title, a.body, when=when)


@bp.route("/actions/<name>", methods=["POST"])
@standard("write")
def std_action(name: str):
    from ..services import api_actions
    who = du_api.identity()
    body = request.get_json(silent=True) if request.get_data() else {}
    status, out = api_actions.run(_db(), name, body, token_id=who.token_id, token_name=who.name or "A token",
                                  key=request.headers.get("Idempotency-Key"))
    resp = jsonify(out)
    resp.status_code = status
    return resp


# -- events --------------------------------------------------------------------------------------

@bp.route("/events")
@standard("read")
def std_events():
    from flask import Response

    from .. import db
    from ..services import events
    core = du_api.core()
    release = core.streams.take()
    if release is None:
        return du_api.ApiCore.error("busy", "Too many open event streams. Try again shortly.", 503, retry_after=30)
    raw = request.headers.get("Last-Event-ID") or request.args.get("last_event_id")
    try:
        last = int(raw) if raw not in (None, "") else events.last_id(_db())
    except ValueError:
        release()
        return du_api.ApiCore.error("bad_request", "Last-Event-ID must be a number.", 400)
    path = current_app.config["BS_CONFIG"].db_path

    def generate():
        conn = db.connect(path)          # its own, read-only use: the request's connection closes first
        try:
            yield from du_api.event_stream(lambda after: events.after(conn, after), last, stopping=core.stopping)
        finally:
            conn.close()
    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.call_on_close(release)
    return resp
