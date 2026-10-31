"""UI routes. Thin: every operation goes through services/."""
from __future__ import annotations

from flask import (Blueprint, Response, current_app, flash, redirect, render_template, request,
                   send_file, url_for)

from ..plex.client import PlexError
from ..services import (activity, alerts, api_tokens, backups, binges, completion, geo, history, homeassistant, jobs,
                        libraries, plex_setup, settings, stats, wrapped)

bp = Blueprint("ui", __name__)

SETUP_ENDPOINTS = {"ui.set_theme", "ui.about_page", "ui.setup_plex", "ui.setup_plex_pin", "ui.setup_plex_pin_check", "ui.setup_plex_save",
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


OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'


@bp.record_once
def _register_globals(state):
    # a global rather than a context variable, so imported macros can call it
    state.app.jinja_env.globals["map_tiles"] = _map_tiles
    state.app.jinja_env.globals["ui_theme"] = _ui_theme


def _ui_theme() -> str:
    try:
        t = settings.get(_db(), "ui.theme")
    except Exception:  # noqa: BLE001 - no database yet
        return "dark"
    return t if t in ("dark", "light", "system") else "dark"


def _map_tiles() -> dict:
    """Basemap for the location map: OpenStreetMap unless Settings names another tile URL."""
    conn = _db()
    url = settings.get(conn, "map.tile_url")
    if not url:
        return {"url": OSM_TILES, "attribution": OSM_ATTRIBUTION, "osm": True}
    return {"url": url, "attribution": settings.get(conn, "map.tile_attribution") or OSM_ATTRIBUTION, "osm": False}


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
        recent=stats.recently_added(conn, 8),
        open_alerts=alerts.list_alerts(conn, limit=5), open_alert_count=alerts.unacked_count(conn),
        no_history=conn.execute("SELECT 1 FROM plays LIMIT 1").fetchone() is None)


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
    # first time through: the "get started" step; a reconnect goes back to Activity
    first = not settings.get(conn, "library.last_sync_at")
    target = url_for("ui.setup_start") if first else url_for("ui.activity")
    if request.headers.get("HX-Request"):
        return "", 204, {"HX-Redirect": target}
    return redirect(target)


@bp.route("/setup/start")
def setup_start():
    return render_template("pages/setup_start.html", step=3)


@bp.route("/setup/start/progress")
def setup_progress():
    conn = _db()
    srv = conn.execute("SELECT name, is_up FROM servers ORDER BY id LIMIT 1").fetchone()
    return render_template("partials/setup_progress.html", server=srv,
                           lib=jobs.latest(conn, "library_sync"), users=jobs.latest(conn, "user_sync"),
                           items=conn.execute("SELECT COUNT(*) FROM media_items WHERE is_present = 1").fetchone()[0],
                           people=conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                           worker=activity.worker_status(conn))


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
    ("binge.gap_min", "Binge: longest gap between episodes (minutes)", "number"),
    ("binge.min_episodes", "Binge: episodes in a row to count", "number"),
    ("abandoned.days", "Show counts as abandoned after (days)", "number"),
    ("library.stale_months", "Library health: stale after (months)", "number"),
    ("ui.public_url", "Public URL for share links (blank: the address you're using)", "text"),
    ("map.tile_url", "Map tiles URL (blank for OpenStreetMap)", "text"),
    ("backup.keep", "Backups to keep", "number"),
    ("map.tile_attribution", "Map tiles attribution (HTML)", "text"),
]
BINGE_KEYS = ("binge.gap_min", "binge.min_episodes")


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
        before = {k: settings.get(conn, k) for k in BINGE_KEYS}
        for key, label, kind in EDITABLE:
            if kind == "bool":
                value = request.form.get(key) == "on"
            else:
                value = request.form.get(key, "")
            if key == "ui.public_url" and value.strip():
                value = value.strip().rstrip("/")
                if not value.startswith(("http://", "https://")):
                    errors.append(f"{label}: start it with https://")
                    continue
            if key == "map.tile_url" and value.strip():
                value = value.strip()
                if not value.startswith("https://") or not all(p in value for p in ("{z}", "{x}", "{y}")):
                    errors.append(f"{label}: needs https:// and {{z}}, {{x}} and {{y}} placeholders")
                    continue
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
        if any(settings.get(conn, k) != v for k, v in before.items()):
            jobs.ensure(conn, "rebuild_binges")   # the definition changed, so recount every binge
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
                           recent_jobs=jobs.recent(conn, 8), ha=homeassistant.view(conn),
                           backup_list=backups.list_backups(current_app.config["BS_CONFIG"].data_dir),
                           backup_keep=settings.get(conn, "backup.keep"),
                           backup_last=settings.get(conn, "backup.last_run_at"))


@bp.route("/settings/homeassistant", methods=["POST"])
def settings_homeassistant():
    try:
        homeassistant.save(_db(), request.form)
        flash("Home Assistant settings saved. The worker picks them up within a few seconds.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("ui.settings_page") + "#homeassistant")


@bp.route("/about")
def about_page():
    import platform
    import sqlite3 as _sqlite3

    from .. import db as dbmod
    conn = _db()
    cfg = current_app.config["BS_CONFIG"]

    def size(p):
        try:
            if p.is_file():
                return p.stat().st_size
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0
        except OSError:
            return 0
    count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    srv = conn.execute("SELECT name, version FROM servers ORDER BY id LIMIT 1").fetchone()
    info = {
        "version": current_app.config["BS_VERSION"], "sha": current_app.config["BS_SHA"],
        "schema": dbmod.current_version(conn), "python": platform.python_version(), "sqlite": _sqlite3.sqlite_version,
        "plex": dict(srv) if srv else None,
        "db_bytes": size(cfg.db_path) + size(cfg.db_path.with_name(cfg.db_path.name + "-wal")),
        "cache_bytes": size(cfg.cache_dir), "backup_bytes": size(backups.backup_dir(cfg.data_dir)),
        "plays": count("SELECT COUNT(*) FROM plays WHERE group_id IS NULL"),
        "users": count("SELECT COUNT(*) FROM users"),
        "library_items": count("SELECT COUNT(*) FROM media_items WHERE is_present = 1"),
        "first_play": count("SELECT MIN(started_at) FROM plays"),
        "worker": activity.worker_status(conn),
    }
    return render_template("pages/about.html", info=info)


@bp.route("/settings/theme", methods=["POST"])
def set_theme():
    theme = request.form.get("theme")
    if theme in ("dark", "light", "system"):
        settings.set(_db(), "ui.theme", theme)
    nxt = request.form.get("next") or "/"
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("ui.index"))


@bp.route("/settings/backups", methods=["POST"])
def settings_backup_now():
    job_id = jobs.ensure(_db(), "backup")
    return redirect(url_for("ui.job_page", job_id=job_id))


@bp.route("/settings/backups/<name>")
def settings_backup_download(name: str):
    path = backups.path_for(current_app.config["BS_CONFIG"].data_dir, name)
    if path is None:
        return render_template("pages/not_found.html", what="backup"), 404
    return send_file(path, as_attachment=True, download_name=name, mimetype="application/vnd.sqlite3")


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
                                  keep_history=request.form.get("keep_history") == "on",
                                  max_streams=request.form.get("max_streams"),
                                  max_transcodes=request.form.get("max_transcodes"),
                                  limit_action=request.form.get("limit_action", "notify"),
                                  limit_message=request.form.get("limit_message"))
            flash("Saved", "ok")
        except LookupError:
            pass
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("ui.user_page", user_id=user_id))
    user = libraries.get_user(conn, user_id)
    if user is None:
        return render_template("pages/not_found.html", what="user"), 404
    rows, cursor = libraries.user_history(conn, user_id, settings.get(conn, "ui.timezone"))
    return render_template("pages/user.html", user=user, rows=rows, cursor=None,
                           f=history.HistoryFilter(user_id=user_id), more=bool(cursor),
                           locations=geo.locations(conn, user_id=user_id),
                           geo_enabled=settings.get(conn, "geo.enabled"),
                           user_alerts=alerts.list_alerts(conn, include_acked=True, user_id=user_id, limit=10),
                           user_binges=binges.longest(conn, user_id=user_id, limit=5),
                           binge_totals=binges.totals(conn, user_id=user_id), shows=completion.summary(conn, user_id),
                           abandoned_days=settings.get(conn, "abandoned.days"),
                           wrapped_year=(wrapped.years(conn, user_id) or [None])[0])


@bp.route("/users/<int:user_id>/stop", methods=["POST"])
def user_stop(user_id: int):
    try:
        n = activity.stop_user_sessions(_db(), user_id, request.form.get("message", ""))
        msg, kind = (f"Stopped {n} stream{'s' if n != 1 else ''}" if n else "Nothing playing"), "ok"
    except PlexError as e:
        msg, kind = str(e), "error"
    if request.headers.get("HX-Request"):
        return render_template("partials/toast.html", message=msg, kind=kind)
    flash(msg, kind)
    return redirect(url_for("ui.user_page", user_id=user_id))


# -- alerts --------------------------------------------------------------------------

@bp.route("/alerts")
def alerts_page():
    show_all = request.args.get("all") == "1"
    return render_template("pages/alerts.html", alerts=alerts.list_alerts(_db(), include_acked=show_all, limit=200),
                           show_all=show_all)


@bp.route("/alerts/ack", methods=["POST"])
def alerts_ack():
    aid = request.form.get("id")
    alerts.acknowledge(_db(), int(aid) if aid and aid.isdigit() else None)
    back = request.form.get("next") or url_for("ui.alerts_page")
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("ui.alerts_page"))


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
