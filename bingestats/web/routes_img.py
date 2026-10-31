"""Image proxy. The Plex token never reaches the browser: posters, art and
avatars are fetched here, resized by Plex, and cached on disk."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from flask import Blueprint, abort, current_app, request, send_file

from ..plex.client import PlexError, fetch_external_image
from ..services import activity

bp = Blueprint("img", __name__, url_prefix="/img")

WIDTHS = (150, 300, 600, 1200)
SHAPES = {"poster": 1.5, "art": 9 / 16, "square": 1.0}
# Only library artwork paths, e.g. /library/metadata/62949/thumb/1788494594
PLEX_PATH = re.compile(r"^/library/metadata/\d+/(thumb|art|banner|clearLogo|theme)(/\d+)?$")
MAX_AGE = 7 * 24 * 3600


def _snap_width(w: str | None) -> int:
    try:
        n = int(w or 300)
    except ValueError:
        n = 300
    return next((x for x in WIDTHS if x >= n), WIDTHS[-1])


def _cache_path(key: str) -> Path:
    d = current_app.config["BS_CONFIG"].cache_dir / "img"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".jpg")


def _store(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _serve(path: Path):
    resp = send_file(path, mimetype="image/jpeg", max_age=MAX_AGE)
    resp.headers["Cache-Control"] = f"private, max-age={MAX_AGE}"
    return resp


@bp.route("/plex")
def plex_image():
    src = request.args.get("path", "")
    if not PLEX_PATH.match(src):
        abort(404)
    shape = request.args.get("shape", "poster")
    ratio = SHAPES.get(shape, 1.5)
    w = _snap_width(request.args.get("w"))
    h = int(w * ratio)
    cached = _cache_path(f"plex|{src}|{w}|{h}")
    if cached.exists():
        return _serve(cached)
    client = activity.plex_client(current_app.get_db())
    if client is None:
        abort(404)
    try:
        r = client.photo(src, w, h)
    except PlexError:
        abort(404)
    if not r.headers.get("Content-Type", "").startswith("image/"):
        abort(404)
    _store(cached, r.content)
    return _serve(cached)


@bp.route("/user/<int:user_id>")
def user_avatar(user_id: int):
    row = current_app.get_db().execute("SELECT thumb_url FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not row["thumb_url"]:
        abort(404)
    url = row["thumb_url"]
    cached = _cache_path(f"user|{url}")
    if cached.exists():
        return _serve(cached)
    try:
        r = fetch_external_image(url)
    except PlexError:
        abort(404)
    except Exception:  # noqa: BLE001 - network errors
        abort(404)
    _store(cached, r.content)
    return _serve(cached)
