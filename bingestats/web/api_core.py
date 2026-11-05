"""The generic part of the outside API (Design Uniformity API-STANDARD.md, draft 2).

Binge is the pilot: this module is lifted into Design Uniformity as `du_api.py` once it's proven,
so it imports nothing from Binge. It talks to the app only through `init_api(...)` and the
callables passed in.

What it does, for every request under /api/v1/ and /display/:
- finds who's asking: `Authorization: Bearer`, else `?token=` (read tokens only), else an admin
  session (`is_admin_session`);
- refuses write tokens in the URL, bad tokens (with a lockout after 10 in a minute from one
  address), wrong scopes, too many requests per token, and tunnel requests when API_PUBLIC=0;
- adds a weak ETag to every successful GET and answers 304 to a matching If-None-Match;
- keeps the registry behind /api/v1/meta (feeds, actions, displays, event types).

Routes written to the standard are marked with `@standard(scope)` and get the standard's error
shape, `{"error": "...", "code": "..."}`. Older routes the app already had keep theirs: refusals
on them go through the `legacy_error` callable, and they check scope with `identity()`.

The rate limit and the lockout are in memory, per process. With several gunicorn workers they are
that many times looser; the standard accepts that (section 10).
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

from flask import current_app, g, jsonify, request

API_VERSION = 1
CODES = ("bad_request", "unauthorised", "forbidden", "not_found", "rate_limited", "busy", "upstream", "error")
STATUS_CODE = {400: "bad_request", 401: "unauthorised", 403: "forbidden", 404: "not_found", 429: "rate_limited",
               503: "busy", 502: "upstream"}

BAD_TOKEN = "That token isn't valid. Make a new one in Settings → API tokens."
WRITE_IN_URL = "Write tokens go in the Authorization header, never the URL."
NO_TOKEN = "Send a token: Authorization: Bearer <token>, or ?token=<read token>."
READ_ONLY = "This token is read-only. Make a write token in Settings → API tokens."
LOCKED_OUT = "Too many bad tokens from this address. Try again later."
TOO_MANY = "Too many requests with this token. Slow down and try again shortly."
NOT_PUBLIC = "The API isn't available through the tunnel. Use the LAN address."


def iso(ts: float | None) -> str | None:
    """UTC epoch seconds -> '2026-11-05T22:40:00Z' (the standard's time format)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> float | None:
    """'2026-11-05T22:40:00Z' (or with an offset) -> epoch seconds; None if it isn't a time."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass
class Identity:
    kind: str                 # "token" or "session"
    scope: str                # "read" or "write"
    token_id: int | None = None
    name: str = ""
    from_url: bool = False


@dataclass
class Limits:
    bad_per_window: int = 10
    window_s: int = 60
    lockout_s: int = 15 * 60
    per_token_per_min: int = 120


class _Limiter:
    """Bad-token lockout per address and request rate per token, in memory, thread-safe."""

    def __init__(self, limits: Limits, clock: Callable[[], float]):
        self.limits, self.clock = limits, clock
        self._lock = threading.Lock()
        self._bad: dict[str, deque] = defaultdict(deque)
        self._locked: dict[str, float] = {}
        self._hits: dict[int, deque] = defaultdict(deque)

    def locked_for(self, addr: str) -> int:
        """Seconds left on this address's lockout, or 0."""
        with self._lock:
            until = self._locked.get(addr, 0)
            left = until - self.clock()
            if left <= 0:
                self._locked.pop(addr, None)
                return 0
            return int(left) + 1

    def bad_token(self, addr: str) -> int:
        """Count a bad token; returns the lockout in seconds if this one tipped it over, else 0."""
        now = self.clock()
        with self._lock:
            q = self._bad[addr]
            q.append(now)
            while q and q[0] <= now - self.limits.window_s:
                q.popleft()
            if len(q) >= self.limits.bad_per_window:
                q.clear()
                self._locked[addr] = now + self.limits.lockout_s
                return self.limits.lockout_s
            return 0

    def hit(self, token_id: int) -> int:
        """Count a request; returns seconds to wait if the token is over its limit, else 0."""
        now = self.clock()
        with self._lock:
            q = self._hits[token_id]
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self.limits.per_token_per_min:
                return int(q[0] + 60 - now) + 1
            q.append(now)
            return 0


@dataclass
class ApiCore:
    name: str
    title: str
    version: str
    sha: Callable[[], str]
    accent: str
    icon: str
    time_zone: Callable[[], str]
    check_token: Callable[[str], dict | None]
    is_admin_session: Callable[[], bool]
    csrf_ok: Callable[[], bool]
    legacy_error: Callable[[str, str, int], Any]
    public: bool = True
    limits: Limits = field(default_factory=Limits)
    clock: Callable[[], float] = time.time
    prefixes: tuple[str, ...] = ("/api/v1/", "/display/")
    open_endpoints: set[str] = field(default_factory=set)    # no token needed (e.g. a health check)
    unmetered_endpoints: set[str] = field(default_factory=set)  # images and events: not rate limited
    feeds: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    displays: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.limiter = _Limiter(self.limits, self.clock)

    # -- registry, for /api/v1/meta --------------------------------------------------------------

    def add_feed(self, name: str, title: str, refresh_s: int, params: dict | None = None) -> None:
        self.feeds.append({"name": name, "title": title, "refresh_s": refresh_s, "params": params or {}})

    def add_action(self, name: str, title: str, body: dict, when: Callable[[], bool] | None = None) -> None:
        self.actions.append({"name": name, "title": title, "body": body, "_when": when})

    def add_display(self, name: str, title: str, sizes: list[str]) -> None:
        self.displays.append({"name": name, "title": title, "sizes": sizes})

    def add_events(self, types: list[str]) -> None:
        self.events.extend(t for t in types if t not in self.events)

    def meta(self) -> dict:
        return {
            "app": self.name, "name": self.title, "version": self.version, "sha": self.sha(), "api": API_VERSION,
            "accent": self.accent, "icon": self.icon, "time_zone": self.time_zone(),
            "feeds": list(self.feeds),
            "actions": [{k: v for k, v in a.items() if k != "_when"} for a in self.actions
                        if a["_when"] is None or a["_when"]()],
            "displays": list(self.displays),
            "events": list(self.events),
        }

    # -- answers -----------------------------------------------------------------------------------

    @staticmethod
    def error(code: str, message: str, status: int, retry_after: int | None = None):
        resp = jsonify({"error": message, "code": code})
        resp.status_code = status
        if retry_after is not None or status in (429, 503):
            resp.headers["Retry-After"] = str(retry_after if retry_after is not None else 30)
        return resp

    def _refuse(self, code: str, message: str, status: int, retry_after: int | None = None):
        if _is_standard():
            return self.error(code, message, status, retry_after)
        resp = current_app.make_response(self.legacy_error(code, message, status))
        if retry_after is not None:
            resp.headers["Retry-After"] = str(retry_after)
        return resp

    # -- the request gate ----------------------------------------------------------------------------

    def _applies(self) -> bool:
        return request.path.startswith(self.prefixes)

    def before(self):
        if not self._applies():
            return None
        g.api_identity = None
        if not self.public and request.headers.get("CF-Connecting-IP"):
            return self._refuse("forbidden", NOT_PUBLIC, 403)
        if request.endpoint in self.open_endpoints:
            return None
        addr = client_address()
        left = self.limiter.locked_for(addr)
        if left:
            return self._refuse("rate_limited", LOCKED_OUT, 429, left)

        token, from_url = None, False
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
        elif request.args.get("token"):
            token, from_url = request.args["token"].strip(), True
        if token:
            row = self.check_token(token)
            if row is None:
                locked = self.limiter.bad_token(addr)
                if locked:
                    return self._refuse("rate_limited", LOCKED_OUT, 429, locked)
                return self._refuse("unauthorised", BAD_TOKEN, 401)
            if from_url and row["scope"] == "write":
                return self._refuse("bad_request", WRITE_IN_URL, 400)
            if request.endpoint not in self.unmetered_endpoints:
                wait = self.limiter.hit(row["id"])
                if wait:
                    return self._refuse("rate_limited", TOO_MANY, 429, wait)
            g.api_identity = Identity("token", row["scope"], row["id"], row.get("name", ""), from_url)
        elif self.is_admin_session():
            g.api_identity = Identity("session", "write", None, "Admin session")
        return None

    def after(self, resp):
        if (self._applies() and request.method == "GET" and resp.status_code == 200
                and not resp.is_streamed and not resp.direct_passthrough and resp.headers.get("ETag") is None):
            resp.add_etag(weak=True)
            resp.make_conditional(request)
        return resp


def client_address() -> str:
    """Who to lock out. Through a Cloudflare tunnel every request arrives from cloudflared's address,
    so one stranger's bad tokens would lock out every tunnel reader; Cloudflare sets
    CF-Connecting-IP to the real client and overwrites any copy sent from outside, so use it when
    it's there. (Faking it on the LAN only dodges the lockout, and tokens are 256-bit.)"""
    return (request.headers.get("CF-Connecting-IP") or "").strip() or request.remote_addr or "?"


def core() -> ApiCore:
    return current_app.extensions["du_api"]


def identity() -> Identity | None:
    return g.get("api_identity")


def _is_standard() -> bool:
    fn = current_app.view_functions.get(request.endpoint or "")
    return bool(getattr(fn, "_du_standard", False))


def standard(scope: str = "read"):
    """Mark a route as one of the standard's and check scope; refusals use the standard's shape."""
    if scope not in ("read", "write"):
        raise ValueError(scope)

    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            who = identity()
            if who is None:
                return ApiCore.error("unauthorised", NO_TOKEN, 401)
            if scope == "write":
                if who.scope != "write":
                    return ApiCore.error("forbidden", READ_ONLY, 403)
                if who.kind == "session" and not core().csrf_ok():
                    return ApiCore.error("bad_request", "Your session expired. Reload the page and try again.", 400)
            return fn(*a, **kw)
        wrapper._du_standard = True  # type: ignore[attr-defined]
        return wrapper
    return deco


# -- images (API standard section 6) ---------------------------------------------------------------

IMAGE_WIDTHS = (80, 160, 320, 640, 1280)
IMAGE_MAX_AGE = 86400
IMAGE_CACHE_BYTES = 500 * 1024 * 1024


def snap(n: int) -> int:
    return next((w for w in IMAGE_WIDTHS if w >= n), IMAGE_WIDTHS[-1])


@dataclass(frozen=True)
class ImageAsk:
    """What a reader asked for, after snapping and clamping."""
    width: int | None
    height: int | None
    fmt: str               # webp | jpeg
    quality: int
    gray: bool

    @property
    def box(self) -> int:
        """The size to fetch the original at: the snapped width (or height)."""
        return self.width or self.height or 320

    def key(self, kind: str, ident: str) -> str:
        import hashlib
        raw = f"{kind}|{ident}|{self.width}|{self.height}|{self.fmt}|{self.quality}|{int(self.gray)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:40]


def image_ask(args, accept: str) -> ImageAsk:
    def num(name):
        try:
            return int(args.get(name) or 0) or None
        except ValueError:
            return None
    w, h = num("w"), num("h")
    w = snap(w) if w else None
    h = snap(h) if h and not w else None
    if not w and not h:
        w = 320
    fmt = (args.get("fmt") or "").lower()
    if fmt not in ("webp", "jpeg"):
        fmt = "webp" if "image/webp" in (accept or "") else "jpeg"
    q = num("q") or 80
    return ImageAsk(w, h, fmt, max(40, min(95, q)), args.get("gray") in ("1", "true"))


def render_image(original: bytes, ask: ImageAsk) -> bytes:
    """Resize (never up), optionally greyscale, and encode."""
    import io

    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(original)) as im:
        im = ImageOps.exif_transpose(im)
        if ask.gray:
            im = im.convert("L")
        elif im.mode not in ("RGB", "L"):
            im = im.convert("RGBA" if ask.fmt == "webp" and "A" in im.getbands() else "RGB")
        if ask.width and im.width > ask.width:
            im = im.resize((ask.width, max(1, round(im.height * ask.width / im.width))), Image.LANCZOS)
        elif ask.height and im.height > ask.height:
            im = im.resize((max(1, round(im.width * ask.height / im.height)), ask.height), Image.LANCZOS)
        if ask.fmt == "jpeg" and im.mode == "RGBA":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "WEBP" if ask.fmt == "webp" else "JPEG", quality=ask.quality,
                **({"optimize": True, "progressive": True} if ask.fmt == "jpeg" else {"method": 4}))
        return out.getvalue()


def image_response(cache_dir, kind: str, ident: str, fetch: Callable[[int], bytes | None]):
    """GET /api/v1/images/<kind>/<id>: from the disk cache, else `fetch(box)` (the original, at
    least `box` wide, fetched with the app's own credentials) resized here. 404 if there's none."""
    from pathlib import Path

    from flask import send_file
    ask = image_ask(request.args, request.headers.get("Accept", ""))
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    key = ask.key(kind, ident)
    path = d / f"{key}.{'webp' if ask.fmt == 'webp' else 'jpg'}"
    if not path.exists():
        try:
            original = fetch(ask.box)
        except Exception:  # noqa: BLE001 - any failure upstream is just "no image"
            original = None
        if not original:
            return ApiCore.error("not_found", "There's no image for that.", 404)
        try:
            data = render_image(original, ask)
        except Exception:  # noqa: BLE001 - not an image we can read
            return ApiCore.error("not_found", "There's no image for that.", 404)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    resp = send_file(path, mimetype=f"image/{ask.fmt}", conditional=True, etag=key, max_age=IMAGE_MAX_AGE)
    resp.headers["Cache-Control"] = f"public, max-age={IMAGE_MAX_AGE}"
    resp.headers["Vary"] = "Accept"
    return resp


def prune_image_cache(cache_dir, max_bytes: int = IMAGE_CACHE_BYTES) -> int:
    """Delete the oldest files (by mtime) until the cache is under max_bytes. Returns how many went.
    Run from a background job, never in a request."""
    from pathlib import Path
    d = Path(cache_dir)
    if not d.is_dir():
        return 0
    files = []
    for p in d.iterdir():
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file():
            files.append((st.st_mtime, st.st_size, p))
    total = sum(f[1] for f in files)
    gone = 0
    for _mtime, size, p in sorted(files):
        if total <= max_bytes:
            break
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        gone += 1
    return gone


def cached(resp, refresh_s: int):
    """Cache-Control for status and feeds: private, half the refresh interval."""
    resp.headers["Cache-Control"] = f"private, max-age={max(0, refresh_s // 2)}"
    return resp


def init_api(app, *, name: str, title: str, version: str, sha: Callable[[], str], accent: str, icon: str,
             time_zone: Callable[[], str], check_token: Callable[[str], dict | None],
             is_admin_session: Callable[[], bool], csrf_ok: Callable[[], bool],
             legacy_error: Callable[[str, str, int], Any], limits: Limits | None = None,
             clock: Callable[[], float] | None = None, public: bool | None = None) -> ApiCore:
    """Install the gate on `app` and return the core (also at app.extensions["du_api"]).

    `public` defaults to the API_PUBLIC env var (1 unless it's "0"). Register the standard's
    routes on the app's own blueprint, marked with @standard; add `meta` with `core().meta()`.
    """
    if public is None:
        public = os.environ.get("API_PUBLIC", "1").strip() != "0"
    c = ApiCore(name=name, title=title, version=version, sha=sha, accent=accent, icon=icon, time_zone=time_zone,
                check_token=check_token, is_admin_session=is_admin_session, csrf_ok=csrf_ok,
                legacy_error=legacy_error, public=public, limits=limits or Limits(), clock=clock or time.time)
    app.extensions["du_api"] = c
    app.before_request(c.before)
    app.after_request(c.after)
    return c
