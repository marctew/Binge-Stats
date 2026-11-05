"""The API standard's routes (Design Uniformity API-STANDARD.md), beside Binge's own /api/v1 routes.

Registered on the same `api` blueprint, so the /api/v1 prefix and the login gate's exemption apply.
These answer in the standard's shapes and error shape; routes_api.py keeps Binge's older ones.
Thin: the answers are built in services/api_feeds.py.
"""
from __future__ import annotations

from flask import current_app, jsonify, request

from ..services import api_feeds
from . import api_core
from .api_core import standard
from .routes_api import bp


def _db():
    return current_app.get_db()


@bp.route("/meta")
@standard("read")
def std_meta():
    return jsonify(api_core.core().meta())


@bp.route("/status")
@standard("read")
def std_status():
    conn = _db()
    body = api_feeds.status(conn, api_feeds.public_url(conn, request.host_url))
    return api_core.cached(jsonify(body), body["refresh_s"])
