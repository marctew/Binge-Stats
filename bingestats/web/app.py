from __future__ import annotations

import logging
import os
import subprocess
from datetime import timedelta
from pathlib import Path

from flask import Flask, g

from .. import __version__
from .. import config as config_mod
from .. import db


def _git_sha() -> str:
    sha = os.environ.get("GIT_SHA")
    if sha:
        return sha[:7]
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=Path(__file__).parent, timeout=2).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def build_app(data_dir: str | None = None, *, testing: bool = False) -> Flask:
    cfg = config_mod.load(data_dir)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.migrate_with_lock(cfg.db_path)

    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="bingestats_session",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        TESTING=testing,
        BS_CONFIG=cfg,
        BS_VERSION=__version__,
        BS_SHA=_git_sha(),
        SEND_FILE_MAX_AGE_DEFAULT=3600,
    )

    if cfg.trusted_proxies:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    def get_db():
        if "db" not in g:
            g.db = db.connect(cfg.db_path)
        return g.db

    app.get_db = get_db  # type: ignore[attr-defined]

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    from . import filters
    filters.register(app)

    from .auth import bp as auth_bp, install as install_auth
    from .routes_api import bp as api_bp
    from .routes_img import bp as img_bp
    from .routes_insights import bp as insights_bp
    from .routes_wrapped import bp as wrapped_bp
    from .routes_notify import bp as notify_bp
    from .routes_ui import bp as ui_bp

    install_auth(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(ui_bp)
    app.register_blueprint(img_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(notify_bp)
    app.register_blueprint(insights_bp)
    app.register_blueprint(wrapped_bp)
    return app
