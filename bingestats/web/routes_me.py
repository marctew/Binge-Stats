"""A viewer's own pages (/me): what someone with a Binge Stats login sees.

Everything here is scoped to session["viewer"], the signed-in person's user
id. web/auth.py only lets viewers reach this blueprint (plus artwork and
sign-out), so admin pages and the REST API stay closed to them.
"""
from __future__ import annotations

from datetime import datetime

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
                   session, url_for)

from ..services import accounts, activity, binges, completion, history, libraries, settings, stats, wrapped

bp = Blueprint("me", __name__, url_prefix="/me")


def _db():
    return current_app.get_db()


def _uid() -> int:
    uid = session.get("viewer")
    if not uid:            # the admin poking at /me: show them the login page's choice instead
        abort(404)
    return uid


@bp.route("")
def home():
    conn, uid = _db(), _uid()
    user = libraries.get_user(conn, uid)
    year = datetime.now(stats._tz(conn)).year
    totals = {k: stats.summary(conn, stats.make_range(conn, preset=p), user_id=uid)
              for k, p in (("today", "today"), ("month", "30d"), ("all", "all"))}
    rows, cursor = history.list_history(conn, history.HistoryFilter(user_id=uid), settings.get(conn, "ui.timezone"),
                                        limit=8)
    yr = stats.make_range(conn, preset="1y")
    return render_template(
        "pages/me_home.html", user=user, totals=totals, year=year,
        sessions=[s for s in activity.list_active(conn) if s.user_id == uid],
        top_shows=stats.top(conn, yr, kind="shows", metric="watch_time", limit=6, user_id=uid)["items"],
        top_movies=stats.top(conn, yr, kind="movies", metric="watch_time", limit=6, user_id=uid)["items"],
        user_binges=binges.longest(conn, user_id=uid, limit=5), binge_totals=binges.totals(conn, user_id=uid),
        shows=completion.summary(conn, uid), rows=rows, more=bool(cursor),
        has_wrapped=year in wrapped.years(conn, uid))


@bp.route("/history")
def history_page():
    conn, uid = _db(), _uid()
    f = history.HistoryFilter.from_args(request.args)
    f.user_id = uid                       # whatever the query string says
    rows, cursor = history.list_history(conn, f, settings.get(conn, "ui.timezone"))
    ctx = {"rows": rows, "cursor": cursor, "f": f, "viewer": True, "compact": True,
           "parts_ep": "me.history_parts", "more_ep": "me.history_page"}
    if request.headers.get("HX-Request") and request.args.get("before"):
        return render_template("partials/history_rows.html", **ctx)
    return render_template("pages/me_history.html", media_types=history.MEDIA_TYPES, **ctx)


@bp.route("/history/<int:play_id>/parts")
def history_parts(play_id: int):
    conn, uid = _db(), _uid()
    row = conn.execute("SELECT user_id FROM plays WHERE id = ?", (play_id,)).fetchone()
    if row is None or row["user_id"] != uid:
        abort(404)
    return render_template("partials/history_parts.html", parts=history.group_parts(conn, play_id))


# -- charts: the same shapes as /api/v1/stats, always for this person -------------------

@bp.route("/data/timeseries")
def data_timeseries():
    conn, uid = _db(), _uid()
    metric = request.args.get("metric", "watch_time")
    return jsonify(stats.timeseries(conn, stats.range_from_args(conn, request.args), split="media_type",
                                    metric=metric if metric in stats.METRICS else "watch_time", user_id=uid))


@bp.route("/data/heatmap")
def data_heatmap():
    conn, uid = _db(), _uid()
    return jsonify(stats.heatmap(conn, stats.range_from_args(conn, request.args), metric="watch_time", user_id=uid))


# -- wrapped ------------------------------------------------------------------------------

@bp.route("/wrapped")
@bp.route("/wrapped/<int:year>")
def wrapped_page(year: int | None = None):
    conn, uid = _db(), _uid()
    years = wrapped.years(conn, uid)
    year = year or (years[0] if years else datetime.now(stats._tz(conn)).year)
    data = wrapped.get(conn, uid, year)
    token = wrapped.share_token(conn, current_app.config["SECRET_KEY"], uid, year)
    base = settings.get(conn, "ui.public_url")
    share = (base.rstrip("/") + url_for("wrapped.public_page", token=token)) if base \
        else url_for("wrapped.public_page", token=token, _external=True)

    def img(path, w=300, shape="poster"):
        return url_for("img.plex_image", path=path, w=w, shape=shape) if path else None
    return render_template("pages/wrapped.html", w=data, img=img, public=False, viewer=True, share_url=share,
                           years=years, months=wrapped.month_labels(), days=wrapped.weekday_labels(),
                           busiest=wrapped.busiest_label(data["busiest_day"]["date"]) if data.get("busiest_day") else None)


@bp.route("/wrapped/revoke", methods=["POST"])
def wrapped_revoke():
    conn, uid = _db(), _uid()
    wrapped.revoke(conn, uid)
    flash("Your old share links no longer work. Copy the new one.", "ok")
    year = request.form.get("year", type=int)
    return redirect(url_for("me.wrapped_page", year=year) if year else url_for("me.wrapped_page"))


# -- pins and the calendar (same pages as the admin's, on this person's own pins) ---------

@bp.route("/calendar")
def calendar_page():
    from .routes_pins import render_calendar
    return render_calendar(_uid(), viewer=True)


@bp.route("/shows")
def shows_page():
    from .routes_pins import render_shows
    return render_shows(_uid(), viewer=True)


@bp.route("/shows/pin-all", methods=["POST"])
def shows_pin_all():
    from .routes_pins import do_pin_all
    return do_pin_all(_uid(), viewer=True)


@bp.route("/shows/undo", methods=["POST"])
def shows_undo():
    from .routes_pins import do_undo
    return do_undo(_uid(), viewer=True)


@bp.route("/calendar/body")
def calendar_body():
    from .routes_pins import render_calendar
    return render_calendar(_uid(), viewer=True, body_only=True)


@bp.route("/calendar/month")
def calendar_month():
    from .routes_pins import render_month
    return render_month(_uid(), viewer=True)


@bp.route("/calendar/find")
def calendar_find():
    from .routes_pins import render_find
    return render_find(_uid(), viewer=True)


@bp.route("/pins/<int:tvdb_id>", methods=["POST"])
def pin_toggle(tvdb_id: int):
    from .routes_pins import do_toggle
    return do_toggle(_uid(), tvdb_id, viewer=True)


# -- account ------------------------------------------------------------------------------

@bp.route("/account", methods=["GET", "POST"])
def account():
    conn, uid = _db(), _uid()
    if request.method == "POST":
        new, again = request.form.get("new", ""), request.form.get("again", "")
        try:
            if new != again:
                raise ValueError("The new passwords don't match")
            session["viewer_v"] = accounts.change_own_password(conn, uid, request.form.get("current", ""), new)
            flash("Password changed", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("me.account"))
    return render_template("pages/me_account.html", user=libraries.get_user(conn, uid), login=accounts.get(conn, uid))


@bp.route("/theme", methods=["POST"])
def theme():
    _uid()
    t = request.form.get("theme")
    if t in ("dark", "light", "system"):
        session["theme"] = t              # theirs alone, in their session; the admin's setting is untouched
    nxt = request.form.get("next") or url_for("me.home")
    return redirect(nxt if nxt.startswith("/me") else url_for("me.home"))
