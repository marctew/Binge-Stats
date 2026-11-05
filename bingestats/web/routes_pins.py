"""Pins and the pinned calendar (docs/specs/pins.md).

The admin pins as the Plex server owner; viewers get the same pages under /me
(routes_me.py), on their own pins. Both go through the helpers here, so the
two can't drift apart. Routes stay thin: everything is in services/pins.py.
"""
from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from ..services import arr, pins
from ..services import shows as shows_svc

bp = Blueprint("pins", __name__)


def _db():
    return current_app.get_db()


def render_calendar(user_id: int | None, *, viewer: bool, body_only: bool = False):
    """The calendar page, or just its body (body_only), which the page reloads after a pin changes."""
    conn = _db()
    cal = pins.calendar(conn, user_id) if user_id is not None else None
    mon = pins.month(conn, user_id, request.args.get("m")) if user_id is not None else None
    if body_only:
        return render_template("partials/calendar_body.html", cal=cal, mon=mon, viewer=viewer) if cal else ""
    return render_template("pages/calendar.html", cal=cal, mon=mon, viewer=viewer, no_owner=user_id is None,
                           sonarr=arr.configured(conn)["sonarr"],
                           find_url=url_for("me.calendar_find" if viewer else "pins.find"),
                           body_url=url_for("me.calendar_body" if viewer else "pins.calendar_body"))


def render_find(user_id: int | None, *, viewer: bool):
    if user_id is None:
        return ""
    q = request.args.get("q", "")
    return render_template("partials/pin_find.html", q=q, hits=pins.find(_db(), q, user_id), viewer=viewer)


def do_toggle(user_id: int | None, tvdb_id: int, *, viewer: bool):
    if user_id is None:
        abort(409, "Pins need the Plex server owner in Users. Sync users from Settings first.")
    try:
        now_pinned = pins.toggle(_db(), user_id, tvdb_id)
    except ValueError as e:
        abort(404, str(e))
    if request.headers.get("HX-Request"):
        # pins-changed: the calendar reloads its body and search results, so a new pin shows at once
        html = render_template("partials/pin_button.html", tvdb_id=tvdb_id, pinned=now_pinned, viewer=viewer,
                               compact=request.args.get("compact") == "1", overlay=request.args.get("overlay") == "1")
        return html, {"HX-Trigger": "pins-changed"}
    nxt = request.form.get("next") or ""
    home = url_for("me.calendar_page" if viewer else "pins.calendar_page")
    if viewer and not nxt.startswith("/me"):
        nxt = home
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else home)


@bp.route("/calendar")
def calendar_page():
    return render_calendar(pins.owner_id(_db()), viewer=False)


@bp.route("/calendar/body")
def calendar_body():
    return render_calendar(pins.owner_id(_db()), viewer=False, body_only=True)


def render_month(user_id: int | None, *, viewer: bool):
    if user_id is None:
        return ""
    return render_template("partials/calendar_month.html", mon=pins.month(_db(), user_id, request.args.get("m")),
                           viewer=viewer)


@bp.route("/calendar/month")
def calendar_month():
    return render_month(pins.owner_id(_db()), viewer=False)


@bp.route("/calendar/find")
def find():
    return render_find(pins.owner_id(_db()), viewer=False)


@bp.route("/pins/<int:tvdb_id>", methods=["POST"])
def toggle(tvdb_id: int):
    return do_toggle(pins.owner_id(_db()), tvdb_id, viewer=False)


# -- the shows browser -----------------------------------------------------------------------

def _ep(viewer: bool, name: str) -> str:
    return f"me.{name}" if viewer else f"pins.{name}"


def render_shows(user_id: int | None, *, viewer: bool):
    if user_id is None:
        return render_calendar(None, viewer=viewer)          # says why: no owner to pin for
    f = shows_svc.Filter.from_args(request.args)
    data = shows_svc.browse(_db(), user_id, f)
    return render_template("pages/shows.html", d=data, f=f, viewer=viewer, sorts=shows_svc.SORTS,
                           shows_ep=_ep(viewer, "shows_page"), pin_all_ep=_ep(viewer, "shows_pin_all"),
                           undo_ep=_ep(viewer, "shows_undo"))


def do_pin_all(user_id: int | None, *, viewer: bool):
    if user_id is None:
        abort(409, "Pins need the Plex server owner in Users. Sync users from Settings first.")
    f = shows_svc.Filter.from_args(request.form)
    n = shows_svc.pin_all(_db(), user_id, f)
    flash(f"Pinned {n:,} show{'s' if n != 1 else ''}" if n else "They were all pinned already", "ok")
    return redirect(url_for(_ep(viewer, "shows_page"), **f.query()))


def do_undo(user_id: int | None, *, viewer: bool):
    if user_id is None:
        abort(409)
    n = shows_svc.undo_last(_db(), user_id)
    flash(f"Unpinned the {n:,} show{'s' if n != 1 else ''} from the last bulk pin" if n else "Nothing to undo", "ok")
    f = shows_svc.Filter.from_args(request.form)
    return redirect(url_for(_ep(viewer, "shows_page"), **f.query()))


@bp.route("/shows")
def shows_page():
    return render_shows(pins.owner_id(_db()), viewer=False)


@bp.route("/shows/pin-all", methods=["POST"])
def shows_pin_all():
    return do_pin_all(pins.owner_id(_db()), viewer=False)


@bp.route("/shows/undo", methods=["POST"])
def shows_undo():
    return do_undo(pins.owner_id(_db()), viewer=False)
