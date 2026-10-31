"""Jinja filters and globals: durations, local times, quality labels."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import current_app, g

from ..services import settings
from ..transcode import REASON_LABELS


def _tz() -> ZoneInfo:
    if "tz" not in g:
        try:
            g.tz = ZoneInfo(settings.get(current_app.get_db(), "ui.timezone"))
        except Exception:  # noqa: BLE001
            g.tz = ZoneInfo("Europe/London")
    return g.tz


def duration(seconds, short: bool = False) -> str:
    if seconds is None:
        return "–"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h >= 24 and not short:
        d, h = divmod(h, 24)
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m" if short else f"{m}m {sec:02d}s"
    return f"{sec}s"


def clock(seconds) -> str:
    """1:02:03 / 4:05 for progress labels."""
    if seconds is None:
        return "–"
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def localtime(ts, fmt: str = "%d %b %Y, %H:%M") -> str:
    if not ts:
        return "–"
    return datetime.fromtimestamp(int(ts), _tz()).strftime(fmt)


def relative(ts) -> str:
    if not ts:
        return "never"
    diff = int(time.time()) - int(ts)
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    if diff < 86400 * 7:
        return f"{diff // 86400}d ago"
    return localtime(ts, "%d %b %Y")


def resolution(value) -> str:
    if not value:
        return ""
    v = str(value).lower()
    if v in ("4k", "8k", "2k"):
        return v.upper()
    if v == "sd":
        return "SD"
    return f"{v}p" if v.isdigit() else v


def codec(value) -> str:
    if not value:
        return ""
    names = {"h264": "H.264", "hevc": "HEVC", "h265": "HEVC", "av1": "AV1", "mpeg2video": "MPEG-2",
             "vc1": "VC-1", "eac3": "E-AC3", "ac3": "AC3", "aac": "AAC", "truehd": "TrueHD",
             "dca": "DTS", "dts": "DTS", "flac": "FLAC", "mp3": "MP3", "opus": "Opus", "vp9": "VP9"}
    return names.get(str(value).lower(), str(value).upper())


def channels(value) -> str:
    if not value:
        return ""
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(int(value), f"{value}ch")


def plural(n, word: str, plural_word: str | None = None) -> str:
    """'1 play', '3 plays', '1,234 plays'."""
    n = int(n or 0)
    return f"{n:,} {word if n == 1 else (plural_word or word + 's')}"


def filesize(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def bandwidth(kbps) -> str:
    if not kbps:
        return "0 Mbps"
    mbps = kbps / 1000
    return f"{mbps:.1f} Mbps" if mbps < 100 else f"{mbps:.0f} Mbps"


DECISION_LABELS = {"direct_play": "Direct Play", "direct_stream": "Direct Stream", "transcode": "Transcode"}


def decision_label(value) -> str:
    return DECISION_LABELS.get(value or "", value or "–")


def reason_label(code) -> str:
    return REASON_LABELS.get(code, code)


def media_title(p) -> str:
    """'Show · S03E02 · The Boy', 'Film (2024)', 'Artist · Track'."""
    get = p.get if isinstance(p, dict) else lambda k: getattr(p, k, None)
    mt = get("media_type")
    if mt == "episode":
        return get("grandparent_title") or get("title") or "Unknown"
    if mt == "track":
        return get("title") or "Unknown"
    return get("title") or "Unknown"


def media_subtitle(p) -> str:
    get = p.get if isinstance(p, dict) else lambda k: getattr(p, k, None)
    mt = get("media_type")
    if mt == "episode":
        se = ""
        if get("season_index") is not None and get("episode_index") is not None:
            se = f"S{int(get('season_index')):02d}E{int(get('episode_index')):02d}"
        return " · ".join(x for x in (se, get("title")) if x)
    if mt == "track":
        return " · ".join(x for x in (get("grandparent_title"), get("parent_title")) if x)
    return str(get("year") or "")


def register(app) -> None:
    for name, fn in {
        "duration": duration, "clock": clock, "localtime": localtime, "relative": relative,
        "resolution": resolution, "codec": codec, "channels": channels, "bandwidth": bandwidth, "filesize": filesize, "plural": plural,
        "decision_label": decision_label, "reason_label": reason_label,
        "media_title": media_title, "media_subtitle": media_subtitle,
    }.items():
        app.jinja_env.filters[name] = fn
    app.jinja_env.globals["now"] = lambda: int(time.time())
