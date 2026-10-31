"""Overall decision and inferred transcode reasons (SPEC.md section 6).

Plex reports *what* it decided for each stream but not *why*, so the reasons
are inferred by comparing the source file with the delivered stream. Pure
functions, shared by the live poller and the Tautulli importer.
"""
from __future__ import annotations

RESOLUTION_RANK = {"sd": 0, "480": 1, "576": 2, "720": 3, "1080": 4, "2k": 5, "4k": 6, "8k": 7}

# Reasons that are about the file itself (re-encoding it would help), as
# opposed to the client's quality setting or bandwidth.
FILE_REASONS = {"video_codec", "hdr_tonemap", "audio_codec", "subtitle_burn", "container"}

REASON_LABELS = {
    "subtitle_burn": "Subtitles burned in",
    "video_codec": "Video codec not supported",
    "video_resolution": "Downscaled (quality setting)",
    "video_bitrate": "Bitrate reduced (quality setting)",
    "hdr_tonemap": "HDR tone mapping",
    "audio_codec": "Audio codec not supported",
    "audio_channels": "Audio downmixed",
    "container": "Container remuxed",
    "transcoder_slow": "Transcoder below real time",
}


def normalise_decision(value: str | None) -> str | None:
    """Map Plex / Tautulli stream decisions to: direct play | copy | transcode."""
    if not value:
        return None
    v = value.strip().lower().replace("_", " ")
    if v in ("directplay", "direct play"):
        return "direct play"
    if v in ("copy", "direct stream", "directstream"):
        return "copy"
    if v == "transcode":
        return "transcode"
    if v == "burn":
        return "burn"
    return v


def resolution_from_size(width: int | None, height: int | None) -> str | None:
    """Plex-style label from frame size. Uses width too, so scope 1920x800 is 1080."""
    w = int(width or 0)
    h = int(height or 0)
    if not w and not h:
        return None
    key = max(h, w * 9 / 16)
    if key >= 3000:
        return "8k" if key >= 4000 else "4k"
    if key >= 1300:
        return "4k" if key >= 1900 else "2k"
    if key >= 900:
        return "1080"
    if key >= 650:
        return "720"
    if key >= 540:
        return "576"
    if key >= 440:
        return "480"
    return "sd"


def normalise_resolution(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    v = str(value).lower().rstrip("p")
    if v in RESOLUTION_RANK:
        return v
    if v.isdigit():
        n = int(v)
        return resolution_from_size(None, n)
    return v


def is_hdr(dynamic_range: str | None) -> bool:
    return bool(dynamic_range) and dynamic_range.upper() != "SDR"


def overall_decision(video: str | None, audio: str | None, subtitle: str | None,
                     has_transcode_session: bool) -> str:
    if video == "transcode" or audio == "transcode" or subtitle == "burn":
        return "transcode"
    if has_transcode_session or video == "copy" or audio == "copy":
        return "direct_stream"
    return "direct_play"


def infer_reasons(*, video: str | None, audio: str | None, subtitle: str | None,
                  has_transcode_session: bool,
                  src_container: str | None, src_video_codec: str | None,
                  src_resolution: str | None, src_dynamic_range: str | None,
                  src_audio_codec: str | None, src_audio_channels: int | None,
                  src_bitrate_kbps: int | None,
                  dst_container: str | None, dst_video_codec: str | None,
                  dst_resolution: str | None, dst_audio_codec: str | None,
                  dst_audio_channels: int | None, dst_bitrate_kbps: int | None,
                  speed: float | None = None, throttled: bool = False) -> list[str]:
    reasons: list[str] = []

    def differs(a, b) -> bool:
        return bool(a) and bool(b) and str(a).lower() != str(b).lower()

    if subtitle == "burn":
        reasons.append("subtitle_burn")

    if video == "transcode":
        before = len(reasons)
        if differs(src_video_codec, dst_video_codec):
            reasons.append("video_codec")
        sr = RESOLUTION_RANK.get(normalise_resolution(src_resolution) or "", None)
        dr = RESOLUTION_RANK.get(normalise_resolution(dst_resolution) or "", None)
        if sr is not None and dr is not None and dr < sr:
            reasons.append("video_resolution")
        if is_hdr(src_dynamic_range):
            reasons.append("hdr_tonemap")
        if len(reasons) == before and "subtitle_burn" not in reasons:
            reasons.append("video_bitrate")
        if speed is not None and speed < 1.0 and not throttled:
            reasons.append("transcoder_slow")

    if audio == "transcode":
        if differs(src_audio_codec, dst_audio_codec):
            reasons.append("audio_codec")
        if src_audio_channels and dst_audio_channels and int(dst_audio_channels) < int(src_audio_channels):
            reasons.append("audio_channels")

    decision = overall_decision(video, audio, subtitle, has_transcode_session)
    if decision == "direct_stream" and differs(src_container, dst_container):
        reasons.append("container")

    return reasons
