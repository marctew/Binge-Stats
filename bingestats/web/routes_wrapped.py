"""Wrapped pages (SPEC.md section 13): the admin view, and the public share
link that needs no login and can only load its own artwork."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from ..services import settings, stats, wrapped
from .routes_img import plex_image_response

bp = Blueprint("wrapped", __name__, url_prefix="/wrapped")



def _db():
    return current_app.get_db()


def _secret() -> str:
    return current_app.config["SECRET_KEY"]


def _this_year(conn) -> int:
    return datetime.now(stats._tz(conn)).year


@bp.route("")
def index():
    conn = _db()
    years = wrapped.years(conn) or [_this_year(conn)]
    year = request.args.get("year", type=int) or years[0]
    rng = wrapped.year_range(conn, year)
    people = [dict(r) for r in conn.execute(
        "SELECT p.user_id, COALESCE(u.friendly_name, u.username) AS name, SUM(p.play_s) AS play_s, "
        "SUM(CASE WHEN p.group_id IS NULL THEN 1 ELSE 0 END) AS plays FROM plays p "
        "LEFT JOIN users u ON u.id = p.user_id WHERE p.started_at >= ? AND p.started_at < ? "
        "GROUP BY p.user_id ORDER BY play_s DESC", (rng.start, rng.end))]
    total = sum(p["play_s"] or 0 for p in people)
    return render_template("pages/wrapped_index.html", years=years, year=year, people=people, total=total)


def _render(data: dict, img, *, public: bool, share_url: str | None = None, years=None):
    return render_template("pages/wrapped.html", w=data, img=img, public=public, share_url=share_url,
                           years=years or [], months=wrapped.month_labels(), days=wrapped.weekday_labels(),
                           busiest=wrapped.busiest_label(data["busiest_day"]["date"]) if data.get("busiest_day") else None)


@bp.route("/<int:year>/<int:user_id>")
def page(year: int, user_id: int):
    conn = _db()
    if user_id != wrapped.SERVER and conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        return render_template("pages/not_found.html", what="person"), 404
    data = wrapped.get(conn, user_id, year)
    token = wrapped.share_token(conn, _secret(), user_id, year)

    def img(path, w=300, shape="poster"):
        return url_for("img.plex_image", path=path, w=w, shape=shape) if path else None
    base = settings.get(conn, "ui.public_url")
    share = (base.rstrip("/") + url_for("wrapped.public_page", token=token)) if base \
        else url_for("wrapped.public_page", token=token, _external=True)
    return _render(data, img, public=False, share_url=share, years=wrapped.years(conn, user_id))


@bp.route("/<int:user_id>/revoke", methods=["POST"])
def revoke(user_id: int):
    wrapped.revoke(_db(), user_id)
    flash("Old share links no longer work. New ones are ready to copy.", "ok")
    year = request.form.get("year", type=int)
    if year:
        return redirect(url_for("wrapped.page", year=year, user_id=user_id))
    return redirect(url_for("ui.user_page", user_id=user_id) if user_id else url_for("wrapped.index"))


def _from_token(token: str):
    conn = _db()
    found = wrapped.read_token(conn, _secret(), token)
    if found is None:
        abort(404)
    uid, year = found
    return conn, uid, year, wrapped.get(conn, uid, year)


@bp.route("/s/<token>")
def public_page(token: str):
    _conn, _uid, _year, data = _from_token(token)

    def img(path, w=300, shape="poster"):
        return url_for("wrapped.public_image", token=token, path=path, w=w, shape=shape) if path else None
    resp = current_app.make_response(_render(data, img, public=True))
    resp.headers["X-Robots-Tag"] = "noindex"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@bp.route("/s/<token>/img")
def public_image(token: str):
    _conn, _uid, _year, data = _from_token(token)
    path = request.args.get("path", "")
    if path not in wrapped.image_paths(data):
        abort(404)
    return plex_image_response(path, request.args.get("shape", "poster"), request.args.get("w"))
