"""Login, setup gate and CSRF for the UI."""
from __future__ import annotations

import hmac
import secrets
import time

from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   session, url_for)

from ..services import auth as auth_svc

bp = Blueprint("auth", __name__)

# Endpoints reachable without logging in.
PUBLIC = {"auth.login", "auth.setup_admin", "static", "wrapped.public_page", "wrapped.public_image"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def csrf_token() -> str:
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf"] = tok
    return tok


def _check_csrf() -> None:
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400, "Your session expired. Reload the page and try again.")


def install(app) -> None:
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _gate():
        ep = request.endpoint or ""
        if ep.startswith("api."):
            return None  # the API checks bearer tokens / session itself; health is public
        conn = app.get_db()
        if not auth_svc.admin_exists(conn):
            if ep in ("auth.setup_admin", "static"):
                return None
            return redirect(url_for("auth.setup_admin"))
        if ep in PUBLIC:
            if request.method not in SAFE_METHODS:
                _check_csrf()
            return None
        if not session.get("admin"):
            if request.headers.get("HX-Request"):
                return "", 401, {"HX-Redirect": url_for("auth.login")}
            return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
        if request.method not in SAFE_METHODS:
            _check_csrf()
        return None


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("ui.index")


@bp.route("/login", methods=["GET", "POST"])
def login():
    conn = current_app.get_db()
    if request.method == "POST":
        if auth_svc.check_login(conn, request.form.get("username", ""), request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["admin"] = True
            csrf_token()
            return redirect(_safe_next(request.args.get("next")))
        time.sleep(0.5)
        flash("Wrong username or password", "error")
    return render_template("pages/login.html", next=request.args.get("next"))


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup_admin():
    conn = current_app.get_db()
    if auth_svc.admin_exists(conn):
        return redirect(url_for("ui.setup_plex"))
    error = None
    if request.method == "POST":
        _check_csrf()
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if pw != pw2:
            error = "Passwords don't match"
        else:
            try:
                auth_svc.set_admin(conn, request.form.get("username", ""), pw)
            except ValueError as e:
                error = str(e)
            else:
                session.clear()
                session.permanent = True
                session["admin"] = True
                csrf_token()
                return redirect(url_for("ui.setup_plex"))
    return render_template("pages/setup_admin.html", error=error, step=1)
