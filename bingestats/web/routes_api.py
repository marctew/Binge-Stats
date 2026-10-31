"""REST API, /api/v1. M1 ships health and activity; the rest lands with M3."""
from __future__ import annotations

import hashlib
import hmac
import time

from flask import Blueprint, current_app, jsonify, request, session

from ..plex.client import PlexError
from ..services import activity

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _err(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _authorise(scope: str):
    """Bearer token, or a logged-in UI session (with CSRF for writes)."""
    conn = current_app.get_db()
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        digest = hashlib.sha256(auth[7:].strip().encode()).hexdigest()
        row = conn.execute("SELECT id, scope FROM api_tokens WHERE token_hash = ?", (digest,)).fetchone()
        if row is None:
            return _err("unauthorised", "Unknown API token", 401)
        if scope == "write" and row["scope"] != "write":
            return _err("forbidden", "This token is read-only", 403)
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (int(time.time()), row["id"]))
        return None
    if session.get("admin"):
        if scope == "write":
            sent = request.headers.get("X-CSRF-Token", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                return _err("csrf", "Missing CSRF token", 400)
        return None
    return _err("unauthorised", "Send Authorization: Bearer <token>", 401)


@bp.route("/health")
def health():
    conn = current_app.get_db()
    status = activity.worker_status(conn)
    srv = conn.execute("SELECT is_up FROM servers ORDER BY id LIMIT 1").fetchone()
    return jsonify({"ok": True, "worker_alive": status["alive"], "plex_up": bool(srv and srv["is_up"]),
                    "plex_configured": status["plex_configured"],
                    "version": current_app.config["BS_VERSION"]})


@bp.route("/activity")
def api_activity():
    denied = _authorise("read")
    if denied:
        return denied
    sessions = activity.list_active(current_app.get_db())
    return jsonify({
        "totals": activity.totals(sessions),
        "sessions": [
            {**a.session.to_dict(), "user_name": a.user_name, "started_at": a.started_at,
             "paused_s": a.paused_s, "progress_pct": round(a.progress_pct, 1)}
            for a in sessions
        ],
    })


@bp.route("/activity/<session_id>/stop", methods=["POST"])
def api_stop(session_id: str):
    denied = _authorise("write")
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    try:
        activity.stop_session(current_app.get_db(), session_id, body.get("message", ""))
    except LookupError as e:
        return _err("not_found", str(e), 404)
    except PlexError as e:
        return _err("plex_error", str(e), 502)
    return jsonify({"ok": True})

