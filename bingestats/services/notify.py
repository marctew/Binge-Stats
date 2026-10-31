"""Notifications (SPEC.md section 9): trigger -> conditions -> template -> channels.

The worker calls `Notifier.handle(trigger, event)` for every event the poller
and the recently-added digest produce. Matching rules are rendered here and
handed to a sender thread, which delivers through Apprise and logs every
attempt in `notify_log`.

Everything a template or a condition can see is the *context*: a plain,
JSON-serialisable dict built by `build_context`. Conditions may only reference
the whitelisted fields in FIELDS, which is also what the UI's dropdowns show.
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jinja2 import ChainableUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from ..transcode import REASON_LABELS

log = logging.getLogger(__name__)

TITLE_MAX = 250
BODY_MAX = 4000

# -- triggers -------------------------------------------------------------------------

TRIGGERS: dict[str, dict] = {
    "play_start": {"label": "Playback started", "group": "session"},
    "play_stop": {"label": "Playback stopped", "group": "stop"},
    "pause": {"label": "Paused", "group": "session"},
    "resume": {"label": "Resumed", "group": "session"},
    "buffer": {"label": "Buffering repeatedly", "group": "session"},
    "watched": {"label": "Finished watching", "group": "session"},
    "transcode_start": {"label": "Started transcoding", "group": "session"},
    "recently_added": {"label": "Recently added", "group": "added"},
    "server_down": {"label": "Plex server down", "group": "server"},
    "server_up": {"label": "Plex server back", "group": "server"},
    "stream_limit": {"label": "Stream limit reached", "group": "limit"},
    "transcode_limit": {"label": "Transcode limit reached", "group": "limit"},
    "concurrent_ips": {"label": "Same account, different places", "group": "limit"},
    "new_country": {"label": "Played from a new country", "group": "limit"},
}

DECISION_LABELS = {"direct_play": "Direct Play", "direct_stream": "Direct Stream", "transcode": "Transcode"}
MEDIA_TYPES = {"movie": "Film", "episode": "Episode", "track": "Track"}
ADDED_KINDS = {"movie": "Films", "episode": "TV episodes", "album": "Music albums"}

# -- condition fields -------------------------------------------------------------------
# kind -> allowed ops. "choice" kinds get their options from CHOICES.
OPS = {
    "str": ["eq", "ne", "contains"],
    "int": ["gt", "lt", "eq"],
    "bool": ["is_true", "is_false"],
    "list": ["contains"],
    "choice": ["in", "not_in"],
}
OP_LABELS = {"eq": "is", "ne": "is not", "contains": "contains", "gt": "is more than", "lt": "is less than",
             "is_true": "is yes", "is_false": "is no", "in": "is one of", "not_in": "is not one of"}

_SESSION_FIELDS = [
    ("user.id", "Person", "choice", "users"),
    ("item.type", "Media type", "choice", "media_types"),
    ("item.show", "Show or artist", "str", None),
    ("item.title", "Title", "str", None),
    ("library.id", "Library", "choice", "libraries"),
    ("stream.decision", "Stream type", "choice", "decisions"),
    ("stream.reasons", "Transcode reason", "list", "reasons"),
    ("stream.hw", "Hardware transcoding", "bool", None),
    ("player.product", "App", "str", None),
    ("player.name", "Device", "str", None),
    ("location.is_local", "On the home network", "bool", None),
    ("location.country", "Country", "str", None),
    ("progress.pct", "Progress (%)", "int", None),
]
FIELDS: dict[str, list[tuple]] = {
    "session": _SESSION_FIELDS,
    "stop": _SESSION_FIELDS + [("play.watched", "Finished it", "bool", None),
                               ("play.minutes", "Watched for (minutes)", "int", None)],
    "added": [("kind", "What was added", "choice", "added_kinds"), ("library.id", "Library", "choice", "libraries"),
              ("count", "Number of items", "int", None), ("show", "Show or artist", "str", None)],
    "server": [],
    "limit": [("user.id", "Person", "choice", "users"), ("stopped", "Stream was stopped", "bool", None),
              ("count", "Number of streams", "int", None)],
}


def fields_for(trigger: str) -> list[dict]:
    group = TRIGGERS.get(trigger, {}).get("group", "session")
    return [{"field": f, "label": label, "kind": kind, "choices": src, "ops": OPS[kind]}
            for f, label, kind, src in FIELDS[group]]


def choices(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    return {
        "users": [{"value": str(r[0]), "label": r[1]} for r in conn.execute(
            "SELECT id, COALESCE(friendly_name, username, CAST(id AS TEXT)) FROM users "
            "WHERE is_active = 1 OR id IN (SELECT DISTINCT user_id FROM plays) ORDER BY 2 COLLATE NOCASE")],
        "libraries": [{"value": str(r[0]), "label": r[1]} for r in conn.execute(
            "SELECT id, name FROM libraries WHERE is_active = 1 ORDER BY name COLLATE NOCASE")],
        "media_types": [{"value": k, "label": v} for k, v in MEDIA_TYPES.items()],
        "decisions": [{"value": k, "label": v} for k, v in DECISION_LABELS.items()],
        "reasons": [{"value": k, "label": v} for k, v in REASON_LABELS.items()],
        "added_kinds": [{"value": k, "label": v} for k, v in ADDED_KINDS.items()],
    }


def validate_conditions(trigger: str, conditions: list[dict]) -> list[dict]:
    allowed = {f["field"]: f for f in fields_for(trigger)}
    clean = []
    for c in conditions or []:
        f = allowed.get(c.get("field"))
        if f is None:
            raise ValueError(f"'{c.get('field')}' can't be used with {TRIGGERS[trigger]['label'].lower()}")
        op = c.get("op")
        if op not in f["ops"]:
            raise ValueError(f"'{OP_LABELS.get(op, op)}' doesn't work with {f['label'].lower()}")
        value = c.get("value")
        if f["kind"] == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{f['label']} needs a number") from None
        elif f["kind"] == "choice":
            value = [str(v) for v in (value if isinstance(value, list) else [value]) if str(v) != ""]
            if not value:
                raise ValueError(f"Pick at least one {f['label'].lower()}")
        elif f["kind"] == "bool":
            value = None
        else:
            value = str(value or "").strip()
            if not value:
                raise ValueError(f"{f['label']} needs a value")
        clean.append({"field": f["field"], "op": op, "value": value})
    return clean


def _get(ctx: dict, path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def condition_matches(c: dict, ctx: dict) -> bool:
    actual = _get(ctx, c["field"])
    op, value = c["op"], c.get("value")
    if op == "is_true":
        return bool(actual)
    if op == "is_false":
        return not actual
    if op in ("in", "not_in"):
        hit = str(actual) in {str(v) for v in (value or [])}
        return hit if op == "in" else not hit
    if op in ("gt", "lt"):
        try:
            a, v = float(actual), float(value)
        except (TypeError, ValueError):
            return False
        return a > v if op == "gt" else a < v
    if op == "contains":
        if isinstance(actual, list):
            return str(value).lower() in {str(x).lower() for x in actual}
        return str(value).lower() in str(actual or "").lower()
    if op == "eq":
        if isinstance(actual, (int, float)) and not isinstance(actual, bool):
            try:
                return float(actual) == float(value)
            except (TypeError, ValueError):
                return False
        return str(actual or "").lower() == str(value or "").lower()
    if op == "ne":
        return str(actual or "").lower() != str(value or "").lower()
    return False


def conditions_match(conditions: list[dict], ctx: dict) -> bool:
    return all(condition_matches(c, ctx) for c in conditions or [])


# -- templates ----------------------------------------------------------------------------

def _duration(seconds) -> str:
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m" if m else f"{s}s"


def _progress_bar(pct, width: int = 10) -> str:
    try:
        p = max(0.0, min(100.0, float(pct or 0)))
    except (TypeError, ValueError):
        p = 0.0
    filled = round(p / 100 * width)
    return "▓" * filled + "░" * (width - filled)


def _plural(n, word: str, plural_word: str | None = None) -> str:
    n = int(n or 0)
    return f"{n:,} {word if n == 1 else (plural_word or word + 's')}"


ENV = SandboxedEnvironment(autoescape=False, undefined=ChainableUndefined, trim_blocks=True, lstrip_blocks=True)
ENV.filters.update(duration=_duration, progress_bar=_progress_bar, plural=_plural)


def check_template(source: str) -> None:
    """Raises ValueError with a readable message if the template won't compile."""
    try:
        ENV.parse(source or "")
    except TemplateError as e:
        raise ValueError(f"Template error: {e}") from None


def render(source: str, ctx: dict, limit: int) -> str:
    out = ENV.from_string(source or "").render(**ctx).strip()
    return out[:limit]


# -- contexts -------------------------------------------------------------------------------

def _server(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT id, name, version FROM servers ORDER BY id LIMIT 1").fetchone()
    return {"id": row["id"], "name": row["name"] or "Plex", "version": row["version"]} if row else {"name": "Plex"}


def _res(v) -> str:
    if not v:
        return ""
    v = str(v).lower()
    return v.upper() if v in ("4k", "8k", "2k") else ("SD" if v == "sd" else f"{v}p" if v.isdigit() else v)


def _codec(v) -> str:
    names = {"h264": "H.264", "hevc": "HEVC", "av1": "AV1", "aac": "AAC", "ac3": "AC3", "eac3": "E-AC3",
             "truehd": "TrueHD", "dca": "DTS", "flac": "FLAC", "mp3": "MP3", "mpeg2video": "MPEG-2"}
    return names.get(str(v or "").lower(), str(v or "").upper())


def session_context(conn: sqlite3.Connection, s: dict) -> dict:
    st = s.get("stream") or {}
    src = st.get("src") or {}
    user = conn.execute("SELECT COALESCE(friendly_name, username) AS name, username FROM users WHERE id = ?",
                        (s.get("user_id"),)).fetchone()
    lib = conn.execute("SELECT name FROM libraries WHERE id = ?", (s.get("library_id"),)).fetchone()
    geo = conn.execute("SELECT city, country, is_private FROM ip_geo WHERE ip = ?", (s.get("ip_address"),)).fetchone() \
        if s.get("ip_address") else None
    mt = s.get("media_type")
    if mt == "episode":
        display = s.get("grandparent_title") or s.get("title")
        se = (f"S{int(s['season_index']):02d}E{int(s['episode_index']):02d}"
              if s.get("season_index") is not None and s.get("episode_index") is not None else "")
        subtitle = " · ".join(x for x in (se, s.get("title")) if x)
        show = s.get("grandparent_title")
    elif mt == "track":
        display = s.get("title")
        subtitle = " · ".join(x for x in (s.get("grandparent_title"), s.get("parent_title")) if x)
        show = s.get("grandparent_title")
    else:
        display = s.get("title")
        subtitle = str(s.get("year") or "")
        show = None
    reasons = list(st.get("transcode_reasons") or [])
    src_q = " ".join(x for x in (_res(src.get("resolution")), _codec(src.get("video_codec"))) if x)
    dst_q = " ".join(x for x in (_res(st.get("dst_resolution")), _codec(st.get("dst_video_codec"))) if x)
    quality = src_q if st.get("video_decision") != "transcode" else f"{src_q or '?'} → {dst_q or '?'}"
    dur, off = s.get("duration_s") or 0, s.get("view_offset_s") or 0
    place = ", ".join(x for x in ((geo["city"], geo["country"]) if geo and not geo["is_private"] else ()) if x)
    return {
        "user": {"id": s.get("user_id"), "name": (user["name"] if user and user["name"] else s.get("user_title")) or "Someone",
                 "username": (user["username"] if user else None) or s.get("user_title")},
        "item": {"type": mt, "type_label": MEDIA_TYPES.get(mt, mt), "title": s.get("title"), "show": show,
                 "season": s.get("season_index"), "episode": s.get("episode_index"), "year": s.get("year"),
                 "display_title": display or "Something", "subtitle": subtitle, "rating_key": s.get("rating_key")},
        "library": {"id": s.get("library_id"), "name": (lib["name"] if lib else None) or s.get("library_title")},
        "stream": {"decision": st.get("decision"), "decision_label": DECISION_LABELS.get(st.get("decision"), "Unknown"),
                   "reasons": reasons, "reason_labels": [REASON_LABELS.get(r, r) for r in reasons],
                   "reasons_text": ", ".join(REASON_LABELS.get(r, r) for r in reasons) or "none",
                   "hw": bool(st.get("hw_decode") or st.get("hw_encode")), "quality": quality,
                   "bandwidth_mbps": round((s.get("bandwidth_kbps") or 0) / 1000, 1)},
        "player": {"name": s.get("player_title") or s.get("product"), "product": s.get("product"),
                   "platform": s.get("platform"), "device": s.get("device")},
        "location": {"is_local": bool(s.get("is_local")), "ip": s.get("ip_address"),
                     "city": geo["city"] if geo else None, "country": geo["country"] if geo else None, "place": place},
        "progress": {"pct": round(off / dur * 100) if dur else 0, "position": _duration(off), "length": _duration(dur)},
        "state": s.get("state"),
        "_poster": s.get("grandparent_thumb") if mt == "episode" else (s.get("parent_thumb") if mt == "track" else s.get("thumb")),
        "_key": s.get("session_id"),
    }


def build_context(conn: sqlite3.Connection, trigger: str, event: dict) -> dict:
    group = TRIGGERS[trigger]["group"]
    if group in ("session", "stop"):
        ctx = session_context(conn, event.get("session") or {})
        if group == "stop":
            ctx["play"] = {"id": event.get("play_id"), "seconds": event.get("play_s") or 0,
                           "minutes": round((event.get("play_s") or 0) / 60), "duration": _duration(event.get("play_s")),
                           "watched": bool(event.get("watched")), "pct": event.get("percent_complete")}
            ctx["_key"] = f"play:{event.get('play_id')}"
    elif group == "added":
        lib = conn.execute("SELECT id, name FROM libraries WHERE id = ?", (event.get("library_id"),)).fetchone()
        ctx = {"kind": event.get("kind"), "kind_label": ADDED_KINDS.get(event.get("kind"), event.get("kind")),
               "library": {"id": lib["id"] if lib else event.get("library_id"), "name": lib["name"] if lib else None},
               "show": event.get("show"), "season": event.get("season"), "count": len(event.get("items") or []),
               "items": event.get("items") or [], "headline": event.get("headline"),
               "_poster": event.get("poster"), "_key": event.get("key")}
    elif group == "limit":
        ctx = session_context(conn, event.get("session") or {})
        d = event.get("detail") or {}
        ctx.update(_limit_fields(d))
        ctx["_key"] = f"user:{ctx['user']['id']}"
    else:
        srv = event.get("server") or {}
        ctx = {"down_for": event.get("down_for_s") or 0, "error": event.get("error") or "",
               "_poster": None, "_key": f"server:{srv.get('id')}"}
    ctx["server"] = _server(conn)
    ctx["trigger"] = trigger
    ctx["trigger_label"] = TRIGGERS[trigger]["label"]
    ctx["now"] = int(time.time())
    return ctx


def _limit_fields(d: dict) -> dict:
    sessions = d.get("sessions") or []
    return {"count": d.get("count") or len(sessions), "limit": d.get("limit"), "stopped": bool(d.get("stopped")),
            "sessions": [{"title": x.get("title"), "player": x.get("player"), "ip": x.get("ip"),
                          "decision": DECISION_LABELS.get(x.get("decision"), x.get("decision"))} for x in sessions],
            "places_text": " and ".join(p["place"] for p in d.get("places") or []),
            "country": d.get("country"), "city": d.get("city")}


def _sample_session(trigger: str) -> dict:
    ctx = {
        "user": {"id": 1001, "name": "Sarah", "username": "sarah_p"},
        "item": {"type": "episode", "type_label": "Episode", "title": "Hello Goodbye", "show": "Slow Horses",
                 "season": 2, "episode": 1, "year": 2022, "display_title": "Slow Horses",
                 "subtitle": "S02E01 · Hello Goodbye", "rating_key": 0},
        "library": {"id": 5, "name": "TV Programmes"},
        "stream": {"decision": "transcode", "decision_label": "Transcode", "reasons": ["subtitle_burn", "audio_channels"],
                   "reason_labels": ["Subtitles burned in", "Audio downmixed"],
                   "reasons_text": "Subtitles burned in, Audio downmixed", "hw": True,
                   "quality": "1080p HEVC → 720p H.264", "bandwidth_mbps": 4.2},
        "player": {"name": "Living Room TV", "product": "Plex for LG", "platform": "webOS", "device": "LG TV"},
        "location": {"is_local": False, "ip": "81.2.69.160", "city": "London", "country": "United Kingdom",
                     "place": "London, United Kingdom"},
        "progress": {"pct": 42, "position": "23m", "length": "55m"}, "state": "playing",
        "_poster": None, "_key": "sample",
    }
    if TRIGGERS[trigger]["group"] == "stop":
        ctx["play"] = {"id": 0, "seconds": 3120, "minutes": 52, "duration": "52m", "watched": True, "pct": 94}
    return ctx


def sample_context(trigger: str) -> dict:
    group = TRIGGERS[trigger]["group"]
    if group in ("session", "stop"):
        ctx = _sample_session(trigger)
    elif group == "limit":
        ctx = _sample_session(trigger)
        ctx.update(_limit_fields({"count": 3, "limit": 2, "stopped": "x", "country": "Spain", "city": "Málaga",
                                  "sessions": [{"title": "Slow Horses", "player": "Living Room TV", "ip": "81.2.69.160",
                                                "decision": "direct_play"},
                                               {"title": "Dune: Part Two", "player": "iPhone", "ip": "88.12.34.56",
                                                "decision": "transcode"}],
                                  "places": [{"place": "London, United Kingdom"}, {"place": "Málaga, Spain"}]}))
    elif group == "added":
        items = [{"title": f"Episode {n}", "display_title": f"Slow Horses · S05E0{n} · Episode {n}",
                  "season": 5, "episode": n, "year": 2026} for n in (1, 2, 3)]
        ctx = {"kind": "episode", "kind_label": "TV episodes", "library": {"id": 5, "name": "TV Programmes"},
               "show": "Slow Horses", "season": 5, "count": 3, "items": items,
               "headline": "Slow Horses · Season 5 · 3 new episodes", "_poster": None, "_key": "sample"}
    else:
        ctx = {"down_for": 754, "error": "Cannot connect", "_poster": None, "_key": "sample"}
    ctx.update(server={"name": "DS9 - Terok Nor"}, trigger=trigger, trigger_label=TRIGGERS[trigger]["label"],
               now=int(time.time()))
    return ctx


def preview_context(conn: sqlite3.Connection, trigger: str) -> tuple[dict, bool]:
    """The last real event for this trigger if there is one, else a sample. (ctx, is_real)"""
    row = conn.execute("SELECT context FROM notify_samples WHERE trigger = ?", (trigger,)).fetchone()
    if row:
        return json.loads(row["context"]), True
    return sample_context(trigger), False


def variables(ctx: dict, prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten a context into (name, example) pairs for the editor's reference list."""
    out = []
    for k, v in ctx.items():
        if k.startswith("_") or k == "now":
            continue
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            out += variables(v, name + ".")
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            out.append((name, f"list of {len(v)}; each has " + ", ".join(sorted(k2 for k2 in v[0]))))
        else:
            out.append((name, v))
    return out


# -- rules and channels ------------------------------------------------------------------------

def mask_url(url: str) -> str:
    scheme, _, rest = (url or "").partition("://")
    if not rest:
        return "•••"
    return f"{scheme}://{rest[:4]}•••"


def validate_url(url: str) -> str:
    import apprise
    url = (url or "").strip()
    if not url:
        raise ValueError("Paste an Apprise URL, e.g. ntfy://ntfy.example/topic")
    a = apprise.Apprise()
    if not a.add(url):
        raise ValueError("Apprise doesn't recognise that URL. Check the format at github.com/caronc/apprise/wiki")
    return url


def list_channels(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, (SELECT MAX(sent_at) FROM notify_log l WHERE l.channel_id = c.id AND l.ok = 1) AS last_ok, "
        "(SELECT COUNT(*) FROM notify_log l WHERE l.channel_id = c.id AND l.ok = 0 "
        " AND l.sent_at > strftime('%s','now') - 86400) AS fails_24h FROM notify_channels c ORDER BY c.name COLLATE NOCASE"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["masked"] = mask_url(d.pop("apprise_url"))
        d["service"] = d["masked"].split("://")[0]
        out.append(d)
    return out


def save_channel(conn: sqlite3.Connection, name: str, url: str | None, channel_id: int | None = None,
                 enabled: bool = True) -> int:
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give the channel a name")
    if channel_id is None:
        url = validate_url(url or "")
        return conn.execute("INSERT INTO notify_channels (name, apprise_url, enabled) VALUES (?, ?, ?)",
                            (name, url, int(enabled))).lastrowid
    if url:  # blank means keep the stored URL
        conn.execute("UPDATE notify_channels SET apprise_url = ? WHERE id = ?", (validate_url(url), channel_id))
    conn.execute("UPDATE notify_channels SET name = ?, enabled = ? WHERE id = ?", (name, int(enabled), channel_id))
    return channel_id


def delete_channel(conn: sqlite3.Connection, channel_id: int) -> None:
    conn.execute("DELETE FROM notify_channels WHERE id = ?", (channel_id,))
    for r in conn.execute("SELECT id, channel_ids FROM notify_rules").fetchall():
        ids = [i for i in json.loads(r["channel_ids"] or "[]") if i != channel_id]
        conn.execute("UPDATE notify_rules SET channel_ids = ? WHERE id = ?", (json.dumps(ids), r["id"]))


def _rule(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["conditions"] = json.loads(d.get("conditions") or "[]")
    d["channel_ids"] = json.loads(d.get("channel_ids") or "[]")
    d["trigger_label"] = TRIGGERS.get(d["trigger"], {}).get("label", d["trigger"])
    return d


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT r.*, (SELECT MAX(sent_at) FROM notify_log l WHERE l.rule_id = r.id AND l.ok = 1) AS last_sent "
        "FROM notify_rules r ORDER BY r.enabled DESC, r.name COLLATE NOCASE").fetchall()
    return [_rule(r) for r in rows]


def get_rule(conn: sqlite3.Connection, rule_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM notify_rules WHERE id = ?", (rule_id,)).fetchone()
    return _rule(row) if row else None


def save_rule(conn: sqlite3.Connection, data: dict, rule_id: int | None = None) -> int:
    name = (data.get("name") or "").strip()[:80]
    trigger = data.get("trigger")
    if not name:
        raise ValueError("Give the rule a name")
    if trigger not in TRIGGERS:
        raise ValueError("Pick what should trigger it")
    conditions = validate_conditions(trigger, data.get("conditions") or [])
    title, body = data.get("title_template") or "", data.get("body_template") or ""
    if not body.strip() and not title.strip():
        raise ValueError("Write a title or a message")
    check_template(title)
    check_template(body)
    known = {r[0] for r in conn.execute("SELECT id FROM notify_channels")}
    channel_ids = [int(c) for c in data.get("channel_ids") or [] if int(c) in known]
    enabled = bool(data.get("enabled"))
    if enabled and not channel_ids:
        raise ValueError("Pick at least one channel before switching the rule on")
    try:
        cooldown = max(0, min(int(data.get("cooldown_s") or 0), 7 * 86400))
    except (TypeError, ValueError):
        raise ValueError("Cooldown must be a number of seconds") from None
    values = (name, int(enabled), trigger, json.dumps(conditions), title, body, json.dumps(channel_ids), cooldown,
              int(bool(data.get("include_poster"))))
    if rule_id is None:
        return conn.execute(
            "INSERT INTO notify_rules (name, enabled, trigger, conditions, title_template, body_template, channel_ids, "
            "cooldown_s, include_poster, created_at) VALUES (?,?,?,?,?,?,?,?,?, strftime('%s','now'))", values).lastrowid
    conn.execute("UPDATE notify_rules SET name=?, enabled=?, trigger=?, conditions=?, title_template=?, "
                 "body_template=?, channel_ids=?, cooldown_s=?, include_poster=? WHERE id=?", values + (rule_id,))
    return rule_id


def set_rule_enabled(conn: sqlite3.Connection, rule_id: int, enabled: bool) -> None:
    rule = get_rule(conn, rule_id)
    if rule is None:
        raise LookupError("No such rule")
    if enabled and not rule["channel_ids"]:
        raise ValueError("Pick a channel for this rule first")
    conn.execute("UPDATE notify_rules SET enabled = ? WHERE id = ?", (int(enabled), rule_id))


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute("DELETE FROM notify_rules WHERE id = ?", (rule_id,))


def recent_log(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT l.*, r.name AS rule_name, c.name AS channel_name FROM notify_log l "
        "LEFT JOIN notify_rules r ON r.id = l.rule_id LEFT JOIN notify_channels c ON c.id = l.channel_id "
        "ORDER BY l.id DESC LIMIT ?", (limit,))]


def preview(conn: sqlite3.Connection, trigger: str, title: str, body: str, conditions: list[dict]) -> dict:
    ctx, real = preview_context(conn, trigger)
    out = {"real": real, "title": "", "body": "", "error": None, "condition_error": None, "matches": None}
    try:
        out["title"] = render(title, ctx, TITLE_MAX)
        out["body"] = render(body, ctx, BODY_MAX)
    except (TemplateError, TypeError, ValueError) as e:
        out["error"] = f"{e.__class__.__name__}: {e}"
    try:
        out["matches"] = conditions_match(validate_conditions(trigger, conditions), ctx)
    except ValueError as e:
        out["condition_error"] = str(e)
    return out


# -- sending --------------------------------------------------------------------------------------

@dataclass
class Delivery:
    rule_id: int | None
    channel_id: int
    url: str
    title: str
    body: str
    dedupe_key: str | None
    poster: str | None = None


def send_one(url: str, title: str, body: str, attachment: str | None = None) -> tuple[bool, str | None]:
    """Deliver through Apprise. Returns (ok, error text)."""
    import apprise
    a = apprise.Apprise()
    if not a.add(url):
        return False, "Invalid Apprise URL"
    with apprise.LogCapture(level=logging.WARNING) as logs:
        ok = a.notify(title=title or None, body=body or title or " ", attach=attachment)
    err = None if ok else (logs.getvalue().strip().splitlines() or ["Delivery failed"])[-1][:500]
    return bool(ok), err


class Notifier:
    """Owned by the worker. `handle` runs on the poller's thread and does only
    database work; deliveries go to a sender thread with its own connection."""

    def __init__(self, conn: sqlite3.Connection, db_path: Path | str, *,
                 poster_fetch: Callable[[str], bytes | None] | None = None,
                 sender: Callable[[str, str, str, str | None], tuple[bool, str | None]] = send_one,
                 clock: Callable[[], float] = time.time, start_thread: bool = True):
        self.conn = conn
        self.db_path = str(db_path)
        self.poster_fetch = poster_fetch
        self.sender = sender
        self.clock = clock
        self.queue: queue.Queue[Delivery | None] = queue.Queue(maxsize=500)
        self.thread: threading.Thread | None = None
        if start_thread:
            self.thread = threading.Thread(target=self._run, name="notify", daemon=True)
            self.thread.start()

    # called for every event
    def handle(self, trigger: str, event: dict) -> list[Delivery]:
        if trigger not in TRIGGERS:
            return []
        try:
            ctx = build_context(self.conn, trigger, event)
        except Exception:  # noqa: BLE001 - a bad event must never break ingest
            log.exception("couldn't build context for %s", trigger)
            return []
        self.conn.execute(
            "INSERT INTO notify_samples (trigger, context, at) VALUES (?, ?, ?) ON CONFLICT(trigger) DO UPDATE SET "
            "context = excluded.context, at = excluded.at", (trigger, json.dumps(ctx, default=str), int(self.clock())))
        deliveries = self.plan(trigger, ctx)
        for d in deliveries:
            try:
                self.queue.put_nowait(d)
            except queue.Full:
                log.warning("notification queue full, dropping %s", trigger)
        return deliveries

    def plan(self, trigger: str, ctx: dict) -> list[Delivery]:
        rules = self.conn.execute("SELECT * FROM notify_rules WHERE enabled = 1 AND trigger = ?", (trigger,)).fetchall()
        if not rules:
            return []
        channels = {r["id"]: r["apprise_url"] for r in self.conn.execute(
            "SELECT id, apprise_url FROM notify_channels WHERE enabled = 1")}
        now = int(self.clock())
        out = []
        for row in rules:
            rule = _rule(row)
            if not conditions_match(rule["conditions"], ctx):
                continue
            key = f"{trigger}:{ctx.get('_key')}"
            # Cooldown: don't repeat the same rule for the same thing (session,
            # play, server, show) within cooldown_s. 0 sends every time.
            if rule["cooldown_s"] > 0 and self.conn.execute(
                    "SELECT 1 FROM notify_log WHERE rule_id = ? AND dedupe_key = ? AND ok = 1 AND sent_at >= ? LIMIT 1",
                    (rule["id"], key, now - rule["cooldown_s"])).fetchone():
                continue
            try:
                title = render(rule["title_template"] or "", ctx, TITLE_MAX)
                body = render(rule["body_template"] or "", ctx, BODY_MAX)
            except Exception as e:  # noqa: BLE001
                self._log(rule["id"], None, key, False, f"Template error: {e}", "", "")
                continue
            for cid in rule["channel_ids"]:
                if cid in channels:
                    out.append(Delivery(rule["id"], cid, channels[cid], title, body, key,
                                        ctx.get("_poster") if rule["include_poster"] else None))
        return out

    def _log(self, rule_id, channel_id, key, ok, error, title, body, conn=None) -> None:
        (conn or self.conn).execute(
            "INSERT INTO notify_log (rule_id, channel_id, sent_at, dedupe_key, ok, error, title, body) "
            "VALUES (?,?,?,?,?,?,?,?)", (rule_id, channel_id, int(self.clock()), key, int(ok), error, title, body))

    def deliver(self, d: Delivery, conn: sqlite3.Connection) -> bool:
        attachment = None
        tmp = None
        if d.poster and self.poster_fetch:
            try:
                data = self.poster_fetch(d.poster)
                if data:
                    tmp = tempfile.NamedTemporaryFile(prefix="bs-poster-", suffix=".jpg", delete=False)
                    tmp.write(data)
                    tmp.close()
                    attachment = tmp.name
            except Exception as e:  # noqa: BLE001 - no poster is better than no notification
                log.debug("poster fetch failed: %s", e)
        try:
            ok, err = self.sender(d.url, d.title, d.body, attachment)
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{e.__class__.__name__}: {e}"
        finally:
            if tmp is not None:
                Path(tmp.name).unlink(missing_ok=True)
        self._log(d.rule_id, d.channel_id, d.dedupe_key, ok, err, d.title, d.body, conn)
        if not ok:
            log.warning("notification to channel %s failed: %s", d.channel_id, err)
        return ok

    def _run(self) -> None:
        from .. import db
        conn = db.connect(self.db_path)
        while True:
            d = self.queue.get()
            if d is None:
                break
            try:
                self.deliver(d, conn)
            except Exception:  # noqa: BLE001
                log.exception("notification delivery crashed")
        conn.close()

    def stop(self) -> None:
        if self.thread is not None:
            self.queue.put(None)
