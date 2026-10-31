"""REST API, /api/v1 (SPEC.md section 16). JSON only.

Auth: `Authorization: Bearer <token>` (tokens from Settings), or the UI's
session cookie, which is how the dashboard's charts load their data. Writes
from a session also need the X-CSRF-Token header.
"""
from __future__ import annotations

import hmac
from functools import wraps

from flask import Blueprint, current_app, jsonify, request, session

from ..plex.client import PlexError
from ..services import activity, api_tokens, history, libraries, settings, stats

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _err(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _db():
    return current_app.get_db()


def require(scope: str):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                tok = api_tokens.check(_db(), auth[7:])
                if tok is None:
                    return _err("unauthorised", "Unknown API token", 401)
                if scope == "write" and tok["scope"] != "write":
                    return _err("forbidden", "This token is read-only", 403)
            elif session.get("admin"):
                if scope == "write":
                    sent = request.headers.get("X-CSRF-Token", "")
                    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                        return _err("csrf", "Missing CSRF token", 400)
            else:
                return _err("unauthorised", "Send Authorization: Bearer <token>", 401)
            return fn(*a, **kw)
        return wrapper
    return deco


def _int_arg(name: str, default=None, lo=None, hi=None):
    v = request.args.get(name)
    try:
        n = int(v) if v not in (None, "") else default
    except ValueError:
        return default
    if n is not None and lo is not None:
        n = max(lo, n)
    if n is not None and hi is not None:
        n = min(hi, n)
    return n


def _scope_args() -> dict:
    return {"user_id": _int_arg("user_id"), "library_id": _int_arg("library_id")}


def _metric() -> str:
    m = request.args.get("metric", "plays")
    return m if m in stats.METRICS else "plays"


@bp.route("/health")
def health():
    conn = _db()
    status = activity.worker_status(conn)
    srv = conn.execute("SELECT is_up FROM servers ORDER BY id LIMIT 1").fetchone()
    return jsonify({"ok": True, "worker_alive": status["alive"], "plex_up": bool(srv and srv["is_up"]),
                    "plex_configured": status["plex_configured"], "version": current_app.config["BS_VERSION"]})


# -- activity ------------------------------------------------------------------------

@bp.route("/activity")
@require("read")
def api_activity():
    sessions = activity.list_active(_db())
    return jsonify({
        "totals": activity.totals(sessions),
        "sessions": [
            {**a.session.to_dict(), "user_name": a.user_name, "started_at": a.started_at,
             "paused_s": a.paused_s, "progress_pct": round(a.progress_pct, 1)}
            for a in sessions
        ],
    })


@bp.route("/activity/<session_id>/stop", methods=["POST"])
@require("write")
def api_stop(session_id: str):
    body = request.get_json(silent=True) or {}
    try:
        activity.stop_session(_db(), session_id, body.get("message", ""))
    except LookupError as e:
        return _err("not_found", str(e), 404)
    except PlexError as e:
        return _err("plex_error", str(e), 502)
    return jsonify({"ok": True})


# -- history -------------------------------------------------------------------------

@bp.route("/history")
@require("read")
def api_history():
    conn = _db()
    args = dict(request.args)
    if "user_id" in args:
        args["user"] = args.pop("user_id")
    if "cursor" in args:
        args["before"] = args.pop("cursor")
    f = history.HistoryFilter.from_args(args)
    limit = _int_arg("limit", 50, 1, 500)
    rows, cursor = history.list_history(conn, f, settings.get(conn, "ui.timezone"), limit=limit)
    items = []
    for r in rows:
        p = {k: v for k, v in r.play.items() if k not in ("server_id", "session_id")}
        p.update(user_name=r.user_name, parts=r.parts, total_play_s=r.total_play_s, transcode_reasons=r.reasons)
        items.append(p)
    return jsonify({"items": items, "next_cursor": cursor})


# -- users / libraries / items ----------------------------------------------------------

@bp.route("/users")
@require("read")
def api_users():
    return jsonify({"items": [{k: v for k, v in u.items() if k != "share_salt"}
                              for u in libraries.list_users(_db())]})


@bp.route("/users/<int:user_id>")
@require("read")
def api_user(user_id: int):
    u = libraries.get_user(_db(), user_id)
    if u is None:
        return _err("not_found", "No such user", 404)
    u.pop("share_salt", None)
    return jsonify(u)


@bp.route("/users/<int:user_id>/stats")
@require("read")
def api_user_stats(user_id: int):
    conn = _db()
    rng = stats.range_from_args(conn, request.args)
    return jsonify({"summary": stats.summary(conn, rng, user_id=user_id),
                    "heatmap": stats.heatmap(conn, rng, user_id=user_id),
                    "top_shows": stats.top(conn, rng, kind="shows", user_id=user_id, limit=10)["items"],
                    "top_movies": stats.top(conn, rng, kind="movies", user_id=user_id, limit=10)["items"]})


@bp.route("/users/<int:user_id>", methods=["PATCH"])
@require("write")
def api_user_update(user_id: int):
    body = request.get_json(silent=True) or {}
    conn = _db()
    u = libraries.get_user(conn, user_id)
    if u is None:
        return _err("not_found", "No such user", 404)
    libraries.update_user(conn, user_id,
                          friendly_name=body.get("friendly_name", u["friendly_name"]),
                          keep_history=bool(body.get("keep_history", u["keep_history"])))
    return jsonify({"ok": True})


@bp.route("/libraries")
@require("read")
def api_libraries():
    return jsonify({"items": libraries.list_libraries(_db())})


@bp.route("/libraries/<int:library_id>")
@require("read")
def api_library(library_id: int):
    lib = libraries.get_library(_db(), library_id)
    if lib is None:
        return _err("not_found", "No such library", 404)
    return jsonify(lib)


@bp.route("/items/<int:rating_key>")
@require("read")
def api_item(rating_key: int):
    item = libraries.get_item(_db(), rating_key)
    if item is None:
        return _err("not_found", "No such item", 404)
    return jsonify(item)


# -- stats ---------------------------------------------------------------------------

@bp.route("/stats/summary")
@require("read")
def api_summary():
    conn = _db()
    return jsonify(stats.summary(conn, stats.range_from_args(conn, request.args), **_scope_args()))


@bp.route("/stats/timeseries")
@require("read")
def api_timeseries():
    conn = _db()
    return jsonify(stats.timeseries(conn, stats.range_from_args(conn, request.args), metric=_metric(),
                                    group=request.args.get("group"), split=request.args.get("split") or None,
                                    **_scope_args()))


@bp.route("/stats/by")
@require("read")
def api_by_period():
    conn = _db()
    return jsonify(stats.by_period(conn, stats.range_from_args(conn, request.args),
                                   dim=request.args.get("dim", "weekday"), metric=_metric(),
                                   split=request.args.get("split") or None, **_scope_args()))


@bp.route("/stats/heatmap")
@require("read")
def api_heatmap():
    conn = _db()
    m = request.args.get("metric", "watch_time")
    return jsonify(stats.heatmap(conn, stats.range_from_args(conn, request.args),
                                 metric=m if m in stats.METRICS else "watch_time", **_scope_args()))


@bp.route("/stats/breakdown")
@require("read")
def api_breakdown():
    conn = _db()
    return jsonify(stats.breakdown(conn, stats.range_from_args(conn, request.args),
                                   field=request.args.get("field", "platform"), metric=_metric(),
                                   limit=_int_arg("limit", 10, 2, 50), **_scope_args()))


@bp.route("/stats/top")
@require("read")
def api_top():
    conn = _db()
    return jsonify(stats.top(conn, stats.range_from_args(conn, request.args),
                             kind=request.args.get("kind", "shows"), metric=_metric(),
                             limit=_int_arg("limit", 10, 1, 100), **_scope_args()))


@bp.route("/stats/concurrent")
@require("read")
def api_concurrent():
    conn = _db()
    return jsonify(stats.concurrent_series(conn, stats.range_from_args(conn, request.args),
                                           group=request.args.get("group")))


@bp.route("/stats/resolution")
@require("read")
def api_resolution():
    conn = _db()
    return jsonify(stats.resolution_flow(conn, stats.range_from_args(conn, request.args)))
