"""Display pages (API standard section 9): ready-made screens for kiosks, Vital Signs and the widget
display. A read token in the URL (or the admin session, for previewing) and nothing else: no
sidebar, no login prompt, no scroll bars.

The page renders on the server. Unless static=1, a small script reloads its body (the same
template, ?fragment=1) every refresh_s and straight away on a playback or arrival event, and if
Binge stops answering it keeps what it has and says when it last updated.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode

from flask import Blueprint, current_app, render_template, request

from ..services import api_feeds, events
from . import api_core

bp = Blueprint("display", __name__, url_prefix="/display")

ACCENTS = ("violet", "blue", "graphite", "teal", "fuchsia")      # DESIGN-SPEC 3.3; violet is Media, Binge's own
SIZES = [("s", 480), ("m", 1100)]                                # up to this width; wider is "l"
IMAGE_W = {"s": 160, "m": 320, "l": 640}
SIZE_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
EVENT_NAMES = [t for t in events.TYPES if t.startswith("playback.") or t == "arrival"]   # what changes this page


def register(core) -> None:
    core.add_display("now-playing", "Now playing", ["320x240", "800x480", "1920x1080"])


def _options() -> dict:
    """theme, size, scale, accent and static from the query, each with a safe default."""
    a = request.args
    theme = a.get("theme") if a.get("theme") in ("dark", "light") else "dark"
    size = "m"
    m = SIZE_RE.match(a.get("size") or "")
    if m:
        w = int(m.group(1))
        size = next((name for name, upto in SIZES if w <= upto), "l")
    try:
        scale = min(3.0, max(0.5, float(a.get("scale") or 1)))
    except ValueError:
        scale = 1.0
    accent = a.get("accent") if a.get("accent") in ACCENTS else "violet"
    return {"theme": theme, "size": size, "scale": scale, "accent": accent, "static": a.get("static") in ("1", "true")}


def _with_token(path: str, **params) -> str:
    """A URL on this app carrying the display's own token (if it came with one)."""
    tok = request.args.get("token")
    q = {k: v for k, v in params.items() if v is not None}
    if tok:
        q["token"] = tok
    return path + ("?" + urlencode(q) if q else "")


@bp.route("/now-playing")
@api_core.display
def now_playing():
    conn = current_app.get_db()
    opts = _options()
    base = api_feeds.public_url(conn, request.host_url)
    playing = api_feeds.feed(conn, "now_playing", api_feeds.feed_args(conn, base, limit=12))
    arrivals = None if playing["items"] else api_feeds.feed(conn, "arrivals", api_feeds.feed_args(conn, base, limit=3))
    w = IMAGE_W[opts["size"]] * (2 if opts["scale"] > 1.25 else 1)

    def img(path: str | None) -> str | None:
        return _with_token(path, w=min(1280, w)) if path else None

    page = {"playing": playing["items"], "arrivals": arrivals["items"] if arrivals else [], "img": img,
            "generated_at": playing["generated_at"], "refresh_s": playing["refresh_s"]}
    keep = {k: request.args[k] for k in ("theme", "size", "scale", "accent") if k in request.args}
    template = "display/now_playing_body.html" if request.args.get("fragment") == "1" else "display/now_playing.html"
    return render_template(template, opts=opts, page=page, title="Now playing",
                           fragment_url=_with_token(request.path, fragment=1, **keep),
                           events_url=_with_token("/api/v1/events"), event_names=EVENT_NAMES)
