"""Plex JSON -> models. All knowledge of Plex's response shape lives here.

Shapes checked against PMS 1.43 (see docs/verification.md). In a transcoding
session, Plex describes the *delivered* stream in Media/Part/Stream and in
TranscodeSession; the source file has to come from /library/metadata/{key},
which is what `parse_source_media` reads.
"""
from __future__ import annotations

from typing import Any

from ..transcode import (
    infer_reasons,
    normalise_decision,
    normalise_resolution,
    overall_decision,
    resolution_from_size,
)
from .models import PlexSession, ServerIdentity, SourceMedia, StreamInfo


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v)


def _ms_to_s(v: Any) -> int | None:
    n = _int(v)
    return None if n is None else n // 1000


def _selected(items: list[dict] | None) -> dict:
    if not items:
        return {}
    for it in items:
        if it.get("selected"):
            return it
    return items[0]


def _streams(part: dict, stream_type: int) -> list[dict]:
    return [s for s in part.get("Stream", []) or [] if _int(s.get("streamType")) == stream_type]


def _selected_stream(part: dict, stream_type: int) -> dict:
    streams = _streams(part, stream_type)
    for s in streams:
        if s.get("selected"):
            return s
    if stream_type == 3:
        return {}  # no subtitle unless one is selected
    for s in streams:
        if s.get("default"):
            return s
    return streams[0] if streams else {}


def dynamic_range(video_stream: dict) -> str | None:
    if not video_stream:
        return None
    if video_stream.get("DOVIPresent") or video_stream.get("DOVIProfile"):
        return "Dolby Vision"
    trc = (video_stream.get("colorTrc") or "").lower()
    if trc == "smpte2084":
        return "HDR10"
    if trc == "arib-std-b67":
        return "HLG"
    return "SDR"


def _media_resolution(media: dict) -> str | None:
    return normalise_resolution(_s(media.get("videoResolution"))) or resolution_from_size(
        _int(media.get("width")), _int(media.get("height"))
    )


def source_from_media(media: dict) -> SourceMedia:
    part = _selected(media.get("Part"))
    video = _selected_stream(part, 1)
    audio = _selected_stream(part, 2)
    size = sum(_int(p.get("size")) or 0 for p in media.get("Part", []) or []) or None
    return SourceMedia(
        container=_s(media.get("container")) or _s(part.get("container")),
        video_codec=_s(media.get("videoCodec")),
        resolution=_media_resolution(media),
        dynamic_range=dynamic_range(video),
        audio_codec=_s(media.get("audioCodec")) or _s(audio.get("codec")),
        audio_channels=_int(media.get("audioChannels")) or _int(audio.get("channels")),
        bitrate_kbps=_int(media.get("bitrate")),
        size_bytes=size,
    )


def parse_source_media(metadata_json: dict, media_id: str | None) -> SourceMedia | None:
    """Source file details from GET /library/metadata/{rating_key}."""
    items = (metadata_json.get("MediaContainer") or {}).get("Metadata") or []
    if not items:
        return None
    medias = items[0].get("Media") or []
    if not medias:
        return None
    chosen = None
    if media_id is not None:
        chosen = next((m for m in medias if str(m.get("id")) == str(media_id)), None)
    return source_from_media(chosen or medias[0])


def parse_identity(identity_json: dict, root_json: dict | None = None) -> ServerIdentity:
    mc = identity_json.get("MediaContainer") or {}
    root = (root_json or {}).get("MediaContainer") or {}
    return ServerIdentity(
        machine_id=str(mc.get("machineIdentifier") or root.get("machineIdentifier")),
        name=_s(root.get("friendlyName")),
        version=_s(mc.get("version") or root.get("version")),
    )


def build_stream(meta: dict, src: SourceMedia | None = None) -> StreamInfo:
    media = _selected(meta.get("Media"))
    part = _selected(media.get("Part"))
    ts = meta.get("TranscodeSession") or {}
    video_s = _selected_stream(part, 1)
    audio_s = _selected_stream(part, 2)
    sub_s = _selected_stream(part, 3)

    has_ts = bool(ts)
    if has_ts:
        video_dec = normalise_decision(ts.get("videoDecision")) or normalise_decision(video_s.get("decision"))
        audio_dec = normalise_decision(ts.get("audioDecision")) or normalise_decision(audio_s.get("decision"))
        sub_dec = normalise_decision(ts.get("subtitleDecision")) or normalise_decision(sub_s.get("decision"))
    else:
        video_dec = "direct play" if video_s or media.get("videoCodec") else None
        audio_dec = "direct play" if audio_s or media.get("audioCodec") else None
        sub_dec = normalise_decision(sub_s.get("decision")) if sub_s else None

    # Without a library lookup, the best source guess is the session's own Media
    # for a direct play, or what TranscodeSession says the source was.
    if src is None:
        src = source_from_media(media)
        if has_ts:
            src.video_codec = _s(ts.get("sourceVideoCodec")) or src.video_codec
            src.audio_codec = _s(ts.get("sourceAudioCodec")) or src.audio_codec
            if video_dec == "transcode":
                src.resolution = None  # Media describes the output; unknown until looked up
                src.dynamic_range = None
            if audio_dec == "transcode":
                src.audio_channels = None
        from_library = False
    else:
        from_library = True

    if has_ts:
        dst_container = _s(ts.get("container")) or _s(media.get("container"))
        dst_vcodec = (_s(ts.get("videoCodec")) or src.video_codec) if video_dec else None
        if video_dec == "transcode":
            dst_res = resolution_from_size(_int(ts.get("width")), _int(ts.get("height"))) or _media_resolution(media)
            dst_dr = "SDR"
        else:
            dst_res, dst_dr = src.resolution, src.dynamic_range
        dst_acodec = _s(ts.get("audioCodec")) or src.audio_codec
        dst_ach = _int(ts.get("audioChannels")) or src.audio_channels
        dst_bitrate = _int(media.get("bitrate"))
    else:
        dst_container, dst_vcodec, dst_res, dst_dr = src.container, src.video_codec, src.resolution, src.dynamic_range
        dst_acodec, dst_ach, dst_bitrate = src.audio_codec, src.audio_channels, src.bitrate_kbps

    speed = None
    if ts.get("speed") is not None:
        try:
            speed = float(ts.get("speed"))
        except (TypeError, ValueError):
            speed = None
    throttled = bool(ts.get("throttled"))

    hw_decode = bool(ts.get("transcodeHwDecoding")) or bool(ts.get("transcodeHwFullPipeline"))
    hw_encode = bool(ts.get("transcodeHwEncoding")) or bool(ts.get("transcodeHwFullPipeline"))

    stream = StreamInfo(
        decision=overall_decision(video_dec, audio_dec, sub_dec, has_ts),
        video_decision=video_dec,
        audio_decision=audio_dec,
        subtitle_decision=sub_dec,
        hw_decode=hw_decode,
        hw_encode=hw_encode,
        throttled=throttled,
        speed=speed,
        src=src,
        src_from_library=from_library,
        dst_container=dst_container,
        dst_video_codec=dst_vcodec,
        dst_resolution=dst_res,
        dst_dynamic_range=dst_dr,
        dst_audio_codec=dst_acodec,
        dst_audio_channels=dst_ach,
        dst_bitrate_kbps=dst_bitrate,
        subtitle_codec=_s(sub_s.get("codec")) if sub_s else None,
        has_transcode_session=has_ts,
    )
    stream.transcode_reasons = infer_reasons(
        video=video_dec, audio=audio_dec, subtitle=sub_dec, has_transcode_session=has_ts,
        src_container=src.container, src_video_codec=src.video_codec,
        src_resolution=src.resolution, src_dynamic_range=src.dynamic_range,
        src_audio_codec=src.audio_codec, src_audio_channels=src.audio_channels,
        src_bitrate_kbps=src.bitrate_kbps,
        dst_container=dst_container, dst_video_codec=dst_vcodec, dst_resolution=dst_res,
        dst_audio_codec=dst_acodec, dst_audio_channels=dst_ach, dst_bitrate_kbps=dst_bitrate,
        speed=speed, throttled=throttled,
    )
    return stream


STATES = {"playing", "paused", "buffering"}


def parse_session(meta: dict, src: SourceMedia | None = None) -> PlexSession | None:
    session = meta.get("Session") or {}
    player = meta.get("Player") or {}
    user = meta.get("User") or {}
    sid = _s(session.get("id"))
    if not sid:
        return None
    user_id = _int(user.get("id")) or _int(player.get("userID"))
    if user_id is None:
        return None
    is_local = bool(player.get("local")) or session.get("location") == "lan"
    ip = _s(player.get("address"))
    if not is_local:
        ip = _s(player.get("remotePublicAddress")) or ip
    state = (player.get("state") or "playing").lower()
    if state not in STATES:
        state = "playing"
    media = _selected(meta.get("Media"))
    duration_ms = meta.get("duration") or media.get("duration")
    return PlexSession(
        session_id=sid,
        session_key=_s(meta.get("sessionKey")),
        user_id=user_id,
        user_title=_s(user.get("title")),
        user_thumb=_s(user.get("thumb")),
        media_type=_s(meta.get("type")) or "unknown",
        rating_key=_int(meta.get("ratingKey")),
        parent_rating_key=_int(meta.get("parentRatingKey")),
        grandparent_rating_key=_int(meta.get("grandparentRatingKey")),
        guid=_s(meta.get("guid")),
        grandparent_guid=_s(meta.get("grandparentGuid")),
        title=_s(meta.get("title")),
        parent_title=_s(meta.get("parentTitle")),
        grandparent_title=_s(meta.get("grandparentTitle")),
        season_index=_int(meta.get("parentIndex")),
        episode_index=_int(meta.get("index")),
        year=_int(meta.get("year")) or _int(meta.get("parentYear")),
        thumb=_s(meta.get("thumb")),
        parent_thumb=_s(meta.get("parentThumb")),
        grandparent_thumb=_s(meta.get("grandparentThumb")),
        art=_s(meta.get("art")) or _s(meta.get("grandparentArt")),
        library_id=_int(meta.get("librarySectionID")),
        library_title=_s(meta.get("librarySectionTitle")),
        duration_s=_ms_to_s(duration_ms),
        view_offset_s=_ms_to_s(meta.get("viewOffset")) or 0,
        player_title=_s(player.get("title")),
        platform=_s(player.get("platform")),
        product=_s(player.get("product")),
        device=_s(player.get("device")) or _s(player.get("model")),
        machine_id=_s(player.get("machineIdentifier")),
        state=state,
        ip_address=ip,
        is_local=is_local,
        is_relayed=bool(player.get("relayed")),
        is_secure=bool(player.get("secure")),
        bandwidth_kbps=_int(session.get("bandwidth")),
        location=_s(session.get("location")),
        media_id=_s(media.get("id")),
        stream=build_stream(meta, src),
    )


def parse_sessions(sessions_json: dict) -> list[tuple[PlexSession, dict]]:
    """Returns (session, raw metadata) pairs; the raw entry lets the caller
    rebuild the stream once the source file is known."""
    out = []
    for meta in (sessions_json.get("MediaContainer") or {}).get("Metadata") or []:
        s = parse_session(meta)
        if s is not None:
            out.append((s, meta))
    return out


# -- library sync -------------------------------------------------------------------

# Plex type numbers for /library/sections/{id}/all?type=
TYPE_NUMBERS = {"movie": 1, "show": 2, "season": 3, "episode": 4, "artist": 8, "album": 9, "track": 10}
# What we store per library type, parents before children.
LIBRARY_TYPES = {"movie": ["movie"], "show": ["show", "episode"], "artist": ["artist", "album", "track"]}


def parse_sections(sections_json: dict) -> list[dict]:
    out = []
    for d in (sections_json.get("MediaContainer") or {}).get("Directory") or []:
        sid = _int(d.get("key"))
        if sid is None:
            continue
        out.append({"id": sid, "name": d.get("title") or f"Library {sid}", "type": d.get("type") or "unknown"})
    return out


def parse_page(page_json: dict) -> tuple[list[dict], int]:
    """Items on one page of /library/sections/{id}/all, and the library total."""
    mc = page_json.get("MediaContainer") or {}
    total = _int(mc.get("totalSize")) or _int(mc.get("size")) or 0
    return [parse_library_item(m) for m in mc.get("Metadata") or []], total


def parse_library_item(m: dict) -> dict:
    t = m.get("type") or "unknown"
    medias = m.get("Media") or []
    media = medias[0] if medias else {}
    size = sum(_int(p.get("size")) or 0 for md in medias[:1] for p in md.get("Part") or []) or None
    item = {
        "rating_key": _int(m.get("ratingKey")),
        "type": t,
        "guid": _s(m.get("guid")),
        "parent_rating_key": _int(m.get("parentRatingKey")),
        "grandparent_rating_key": _int(m.get("grandparentRatingKey")),
        "title": _s(m.get("title")) or "Unknown",
        "parent_title": _s(m.get("parentTitle")),
        "grandparent_title": _s(m.get("grandparentTitle")),
        "season_index": _int(m.get("parentIndex")) if t == "episode" else None,
        "episode_index": _int(m.get("index")) if t in ("episode", "track") else None,
        "year": _int(m.get("year")) or _int(m.get("parentYear")),
        "duration_s": _ms_to_s(m.get("duration")),
        "added_at": _int(m.get("addedAt")),
        "originally_available_at": _s(m.get("originallyAvailableAt")),
        "thumb": _s(m.get("thumb")),
        "art": _s(m.get("art")) or _s(m.get("grandparentArt")),
        "size_bytes": size,
        "video_codec": _s(media.get("videoCodec")),
        "video_resolution": _media_resolution(media) if media.get("videoCodec") else None,
        "video_dynamic_range": None,   # needs per-item metadata; filled from plays where known
        "audio_codec": _s(media.get("audioCodec")),
        "audio_channels": _int(media.get("audioChannels")),
        "container": _s(media.get("container")),
        "bitrate_kbps": _int(media.get("bitrate")),
    }
    return item


# -- users (plex.tv) ----------------------------------------------------------------

def parse_owner(user_json: dict) -> dict:
    return {"id": _int(user_json.get("id")), "username": _s(user_json.get("username")) or _s(user_json.get("title")),
            "email": _s(user_json.get("email")), "thumb_url": _s(user_json.get("thumb")),
            "is_owner": True, "is_home": bool(user_json.get("home"))}


def parse_home_users(home_json: dict) -> list[dict]:
    out = []
    for u in home_json.get("users") or []:
        uid = _int(u.get("id"))
        if uid is None:
            continue
        out.append({"id": uid, "username": _s(u.get("username")) or _s(u.get("title")),
                    "friendly_name": _s(u.get("friendlyName")), "email": _s(u.get("email")),
                    "thumb_url": _s(u.get("thumb")), "is_owner": bool(u.get("admin")), "is_home": True})
    return out


def parse_shared_users(xml_text: str) -> list[dict]:
    """plex.tv/api/users is XML only (the JSON /api/v2/friends returns 410 Gone)."""
    import xml.etree.ElementTree as ET
    out = []
    root = ET.fromstring(xml_text)
    for u in root.iter("User"):
        uid = _int(u.get("id"))
        if uid is None:
            continue
        out.append({"id": uid, "username": u.get("username") or u.get("title"), "email": u.get("email") or None,
                    "thumb_url": u.get("thumb") or None, "is_owner": False, "is_home": u.get("home") == "1"})
    return out
