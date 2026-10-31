"""UI routes. Thin: every operation goes through services/."""
from __future__ import annotations

from flask import (Blueprint, Response, current_app, flash, redirect, render_template, request,
                   url_for)

from ..plex.client import PlexError
from ..services import activity, api_tokens, geo, history, jobs, libraries, plex_setup, settings, stats

bp = Blueprint("ui", __name__)

SETUP_ENDPOINTS = {"ui.setup_plex", "ui.setup_plex_pin", "ui.setup_plex_pin_check", "ui.setup_plex_save",
                   "ui.settings_page", "ui.job_page", "ui.job_partial"}
DEFAULT_IMPORT_PATH = "/data/import/tautulli.db"


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
    conn = _db()
    tz = stats._tz(conn)
    from datetime import datetime
    hour = datetime.now(tz).hour
    greeting = "Good morning" if 5 <= hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    month = stats.make_range(conn, preset="30d")
    srv = conn.execute("SELECT name FROM servers ORDER BY id LIMIT 1").fetchone()
    return render_template(
        "pages/dashboard.html", greeting=greeting, server_name=(srv["name"] if srv and srv["name"] else "Plex"),
        sessions=activity.list_active(conn),
        today=stats.summary(conn, stats.make_range(conn, preset="today")),
        week=stats.summary(conn, stats.make_range(conn, preset="7d")),
        month=stats.summary(conn, month),
        top_users=stats.top(conn, month, kind="users", metric="watch_time", limit=6)["items"],
        top_shows=stats.top(conn, month, kind="shows", limit=6)["items"],
        top_movies=stats.top(conn, month, kind="movies", limit=6)["items"],
        recent=stats.recently_added(conn, 8))


@bp.route("/dashboard/now")
def dashboard_now():
    return render_template("partials/now_strip.html", sessions=activity.list_active(_db()))


@bp.route("/graphs")
def graphs_page():
    conn = _db()
    preset = request.args.get("range") if request.args.get("range") in (*stats.PRESETS, "all") else "30d"
    metric = request.args.get("metric") if request.args.get("metric") in stats.METRICS else "plays"
    rng = stats.make_range(conn, preset=preset)
    return render_template("pages/graphs.html", preset=preset, metric=metric, rng=rng,
                           summary=stats.summary(conn, rng),
                           unit="hours" if metric == "watch_time" else "plays")


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
    ("library.sync_h", "Sync libraries and users every (hours)", "number"),
]


@bp.route("/settings/tokens", methods=["POST"])
def settings_token_create():
    conn = _db()
    try:
        token = api_tokens.create(conn, request.form.get("name", ""), request.form.get("scope", "read"))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("ui.settings_page") + "#api")
    # Rendered straight into this response, never put in the session cookie.
    return _render_settings(conn, new_token=token, new_token_name=request.form.get("name", "").strip())


@bp.route("/settings/tokens/<int:token_id>/delete", methods=["POST"])
def settings_token_delete(token_id: int):
    api_tokens.revoke(_db(), token_id)
    flash("Token revoked", "ok")
    return redirect(url_for("ui.settings_page") + "#api")


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
    return _render_settings(conn)


def _render_settings(conn, **extra):
    values = {k: settings.get(conn, k) for k, _l, _t in EDITABLE}
    return render_template("pages/settings.html", fields=EDITABLE, values=values, tokens=api_tokens.list_tokens(conn),
                           **extra,
                           plex_url=settings.get(conn, "plex.url"),
                           server=conn.execute("SELECT * FROM servers ORDER BY id LIMIT 1").fetchone(),
                           library_sync=jobs.latest(conn, "library_sync"), user_sync=jobs.latest(conn, "user_sync"),
                           last_library_sync=settings.get(conn, "library.last_sync_at"),
                           recent_jobs=jobs.recent(conn, 8))


@bp.route("/settings/sync", methods=["POST"])
def settings_sync():
    conn = _db()
    kind = request.form.get("kind")
    if kind not in ("library_sync", "user_sync"):
        return redirect(url_for("ui.settings_page"))
    job_id = jobs.ensure(conn, kind)
    return redirect(url_for("ui.job_page", job_id=job_id))


# -- jobs --------------------------------------------------------------------------

@bp.route("/jobs/<int:job_id>")
def job_page(job_id: int):
    job = jobs.get(_db(), job_id)
    if job is None:
        return render_template("pages/not_found.html", what="job"), 404
    return render_template("pages/job.html", job=job)


@bp.route("/jobs/<int:job_id>/partial")
def job_partial(job_id: int):
    job = jobs.get(_db(), job_id)
    if job is None:
        return "", 404
    return render_template("partials/job_body.html", job=job)


# -- import ------------------------------------------------------------------------

@bp.route("/import", methods=["GET", "POST"])
def import_page():
    conn = _db()
    if request.method == "POST":
        path = (request.form.get("path") or DEFAULT_IMPORT_PATH).strip()
        kind = "tautulli_import" if request.form.get("action") == "import" else "tautulli_check"
        try:
            job_id = jobs.create(conn, kind, {"path": path})
        except jobs.JobBusy as e:
            job_id = e.job_id
        return redirect(url_for("ui.job_page", job_id=job_id))
    last_check = jobs.latest(conn, "tautulli_check")
    last_import = jobs.latest(conn, "tautulli_import")
    imported = conn.execute("SELECT COUNT(*) FROM plays WHERE source = 'tautulli'").fetchone()[0]
    return render_template("pages/import.html", default_path=DEFAULT_IMPORT_PATH,
                           last_check=last_check, last_import=last_import, imported=imported)


# -- users -------------------------------------------------------------------------

@bp.route("/users")
def users_page():
    show_all = request.args.get("all") == "1"
    return render_template("pages/users.html", users=libraries.list_users(_db(), include_inactive=show_all),
                           show_all=show_all)


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
def user_page(user_id: int):
    conn = _db()
    if request.method == "POST":
        try:
            libraries.update_user(conn, user_id, friendly_name=request.form.get("friendly_name"),
                                  keep_history=request.form.get("keep_history") == "on")
            flash("Saved", "ok")
        except LookupError:
            pass
        return redirect(url_for("ui.user_page", user_id=user_id))
    user = libraries.get_user(conn, user_id)
    if user is None:
        return render_template("pages/not_found.html", what="user"), 404
    rows, cursor = libraries.user_history(conn, user_id, settings.get(conn, "ui.timezone"))
    return render_template("pages/user.html", user=user, rows=rows, cursor=None,
                           f=history.HistoryFilter(user_id=user_id), more=bool(cursor),
                           locations=geo.locations(conn, user_id=user_id),
                           geo_enabled=settings.get(conn, "geo.enabled"))


# -- libraries ---------------------------------------------------------------------

@bp.route("/libraries")
def libraries_page():
    conn = _db()
    return render_template("pages/libraries.html", libraries=libraries.list_libraries(conn),
                           last_sync=settings.get(conn, "library.last_sync_at"),
                           sync_job=jobs.latest(conn, "library_sync"))


@bp.route("/libraries/<int:library_id>")
def library_page(library_id: int):
    lib = libraries.get_library(_db(), library_id)
    if lib is None:
        return render_template("pages/not_found.html", what="library"), 404
    return render_template("pages/library.html", lib=lib)


@bp.route("/item/<int:rating_key>")
def item_page(rating_key: int):
    item = libraries.get_item(_db(), rating_key)
    if item is None:
        return render_template("pages/not_found.html", what="item"), 404
    return render_template("pages/item.html", item=item)
