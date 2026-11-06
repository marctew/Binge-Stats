from __future__ import annotations

import logging
import os
import subprocess
from importlib import import_module
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


def _install_api(app: Flask) -> None:
    """The outside API standard's gate (api_core): tokens, limits, ETags and the meta registry."""
    import hmac

    from flask import request, session

    from ..services import api_tokens, settings
    from . import api_core
    from .routes_api import _err

    def csrf_ok() -> bool:
        sent = request.headers.get("X-CSRF-Token", "")
        return bool(sent) and hmac.compare_digest(sent, session.get("csrf", ""))

    core = api_core.init_api(
        app, name="binge-stats", title="Binge Stats", version=__version__, sha=lambda: app.config["BS_SHA"],
        accent="violet", icon="/static/img/icon.svg",
        time_zone=lambda: settings.get(app.get_db(), "ui.timezone"),
        check_token=lambda t: api_tokens.check(app.get_db(), t),
        is_admin_session=lambda: bool(session.get("admin")),
        csrf_ok=csrf_ok,
        legacy_error=lambda code, message, status: _err(code, message, status),
        max_streams=4,          # event streams per gunicorn worker: 8 per app across the 2 workers
    )
    core.open_endpoints.add("api.health")
    if not app.testing:
        api_core.stop_streams_on_sigterm(core)


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
    from .routes_me import bp as me_bp
    from .routes_wrapped import bp as wrapped_bp
    from .routes_notify import bp as notify_bp
    from .routes_pins import bp as pins_bp
    from .routes_ui import bp as ui_bp
    from .routes_display import bp as display_bp, register as register_displays

    install_auth(app)
    _install_api(app)
    std = import_module(".routes_api_std", __package__)  # the API standard's routes, added to the api blueprint
    std.register_feeds(app.extensions["du_api"])
    std.register_actions(app.extensions["du_api"], app.get_db)
    app.extensions["du_api"].unmetered_endpoints.update({"api.std_image", "api.std_events"})
    from ..services import events
    app.extensions["du_api"].add_events(events.TYPES)
    app.register_blueprint(auth_bp)
    app.register_blueprint(ui_bp)
    app.register_blueprint(img_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(notify_bp)
    app.register_blueprint(insights_bp)
    app.register_blueprint(wrapped_bp)
    app.register_blueprint(me_bp)
    app.register_blueprint(pins_bp)
    app.register_blueprint(display_bp)
    register_displays(app.extensions["du_api"])

    @app.get("/healthz")
    def healthz():
        """Homelab app standard: up, and the jobs a restart now would cut short (scripts/update.sh asks)."""
        conn = app.get_db()
        busy = [r[0] for r in conn.execute("SELECT DISTINCT kind FROM jobs WHERE status = 'running'")]
        return {"ok": True, "busy": busy}

    try:  # the Design system card in Settings; du_flask.py is copied in by the design updater
        from ..du_flask import init_design
        init_design(app, Path(cfg.data_dir) / "design", version=__version__)
    except ImportError:
        pass
    return app
