"""UI routes. Thin: every operation goes through services/."""
from __future__ import annotations

from flask import (Blueprint, Response, current_app, flash, redirect, render_template, request,
                   url_for)

from ..plex.client import PlexError
from ..services import activity, history, plex_setup, settings

bp = Blueprint("ui", __name__)

SETUP_ENDPOINTS = {"ui.setup_plex", "ui.setup_plex_pin", "ui.setup_plex_pin_check", "ui.setup_plex_save",
                   "ui.settings_page"}


def _db():
    return current_app.get_db()


@bp.before_request
def _need_plex():
    if request.endpoint in SETUP_ENDPOINTS:
        return None
    if not settings.plex_configured(_db()):
        return redirect(url_for("ui.setup_plex"))
    return None


@bp.app_context_processor
def _layout_context():
    try:
        status = activity.worker_status(_db())
    except Exception:  # noqa: BLE001 - no db yet (e.g. error pages)
        status = {"alive": True, "error": "", "plex_configured": True}
    return {"worker": status, "app_version": current_app.config["BS_VERSION"],
            "app_sha": current_app.config["BS_SHA"]}


@bp.route("/")
def index():
    return redirect(url_for("ui.activity"))


# -- activity ----------------------------------------------------------------------

@bp.route("/activity", endpoint="activity")
def activity_page():
    return render_template("pages/activity.html", **_activity_ctx())


@bp.route("/activity/partial")
def activity_partial():
    return render_template("partials/activity_body.html", **_activity_ctx())


def _activity_ctx() -> dict:
    sessions = activity.list_active(_db())
    return {"sessions": sessions, "totals": activity.totals(sessions)}


@bp.route("/activity/<session_id>/stop", methods=["POST"])
def activity_stop(session_id: str):
    try:
        activity.stop_session(_db(), session_id, request.form.get("message", ""))
        msg, kind = "Stream stopped. It will drop off this page in a few seconds.", "ok"
    except (LookupError, PlexError) as e:
        msg, kind = str(e), "error"
    if request.headers.get("HX-Request"):
        return render_template("partials/toast.html", message=msg, kind=kind)
    flash(msg, kind)
    return redirect(url_for("ui.activity"))


# -- history -----------------------------------------------------------------------

@bp.route("/history")
def history_page():
    conn = _db()
    f = history.HistoryFilter.from_args(request.args)
    tz = settings.get(conn, "ui.timezone")
    rows, cursor = history.list_history(conn, f, tz)
    ctx = {"rows": rows, "cursor": cursor, "f": f}
    if request.headers.get("HX-Request") and request.args.get("before"):
        return render_template("partials/history_rows.html", **ctx)
    return render_template("pages/history.html", users=history.users_for_filter(conn),
                           media_types=history.MEDIA_TYPES, decisions=history.DECISIONS, **ctx)


@bp.route("/history/<int:play_id>/parts")
def history_parts(play_id: int):
    return render_template("partials/history_parts.html", parts=history.group_parts(_db(), play_id))


@bp.route("/history.csv")
def history_csv():
    conn = _db()
    f = history.HistoryFilter.from_args(request.args)
    body = history.export_csv(conn, f, settings.get(conn, "ui.timezone"))
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=binge-stats-history.csv"})


# -- setup: plex -------------------------------------------------------------------

@bp.route("/setup/plex")
def setup_plex():
    conn = _db()
    return render_template("pages/setup_plex.html", step=2,
                           current_url=settings.get(conn, "plex.url"),
                           configured=settings.plex_configured(conn))


@bp.route("/setup/plex/pin", methods=["POST"])
def setup_plex_pin():
    try:
        pin = plex_setup.start_pin(_db())
    except PlexError as e:
        return render_template("partials/plex_pin.html", error=str(e))
    return render_template("partials/plex_pin.html", pin=pin)


@bp.route("/setup/plex/pin/<int:pin_id>")
def setup_plex_pin_check(pin_id: int):
    try:
        servers = plex_setup.check_pin(_db(), pin_id)
    except PlexError as e:
        return render_template("partials/plex_pin.html", error=str(e))
    if servers is None:
        return render_template("partials/plex_pin.html", pin={"id": pin_id}, waiting=True)
    return render_template("partials/plex_servers.html", servers=servers)


@bp.route("/setup/plex/save", methods=["POST"])
def setup_plex_save():
    conn = _db()
    url = request.form.get("url", "")
    token = request.form.get("token", "").strip()
    verify = request.form.get("verify_tls", "1") == "1"
    try:
        if token:
            info = plex_setup.save_connection(conn, url, token, verify)
        else:
            info = plex_setup.save_from_pending(conn, plex_setup.normalise_url(url), verify)
    except (PlexError, ValueError) as e:
        if request.headers.get("HX-Request"):
            return render_template("partials/toast.html", message=f"Couldn't connect: {e}", kind="error")
        flash(f"Couldn't connect: {e}", "error")
        return redirect(url_for("ui.setup_plex"))
    flash(f"Connected to {info['name'] or 'Plex'} ({info['version']})", "ok")
    if request.headers.get("HX-Request"):
        return "", 204, {"HX-Redirect": url_for("ui.activity")}
    return redirect(url_for("ui.activity"))


# -- settings ----------------------------------------------------------------------

EDITABLE = [
    ("ui.timezone", "Timezone", "text"),
    ("poll.interval_s", "Poll interval (seconds)", "number"),
    ("poll.use_websocket", "Use Plex websocket for instant updates", "bool"),
    ("ingest.min_play_s", "Ignore plays shorter than (seconds)", "number"),
    ("ingest.grace_polls", "Missed polls before a stream counts as stopped", "number"),
    ("ingest.watched_pct.movie", "Movie counts as watched at (%)", "number"),
    ("ingest.watched_pct.episode", "Episode counts as watched at (%)", "number"),
    ("ingest.watched_pct.track", "Track counts as played at (%)", "number"),
    ("ingest.group_resume_h", "Group resumed plays within (hours)", "number"),
]


@bp.route("/settings", methods=["GET", "POST"])
def settings_page():
    conn = _db()
    if request.method == "POST":
        errors = []
        for key, label, kind in EDITABLE:
            if kind == "bool":
                value = request.form.get(key) == "on"
            else:
                value = request.form.get(key, "")
            if key == "ui.timezone":
                from zoneinfo import ZoneInfo
                try:
                    ZoneInfo(value)
                except Exception:  # noqa: BLE001
                    errors.append(f"{label}: unknown timezone")
                    continue
            try:
                settings.set(conn, key, value)
            except (ValueError, TypeError) as e:
                errors.append(f"{label}: {e}")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            flash("Settings saved", "ok")
        return redirect(url_for("ui.settings_page"))
    values = {k: settings.get(conn, k) for k, _l, _t in EDITABLE}
    return render_template("pages/settings.html", fields=EDITABLE, values=values,
                           plex_url=settings.get(conn, "plex.url"),
                           server=conn.execute("SELECT * FROM servers ORDER BY id LIMIT 1").fetchone())
