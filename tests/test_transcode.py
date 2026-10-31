import pytest

from bingestats.transcode import (infer_reasons, normalise_decision, overall_decision,
                                  resolution_from_size)

BASE = dict(src_container="mkv", src_video_codec="h264", src_resolution="1080", src_dynamic_range="SDR",
            src_audio_codec="ac3", src_audio_channels=6, src_bitrate_kbps=10000,
            dst_container="mkv", dst_video_codec="h264", dst_resolution="1080", dst_audio_codec="ac3",
            dst_audio_channels=6, dst_bitrate_kbps=10000)


def reasons(video="direct play", audio="direct play", subtitle=None, ts=False, **kw):
    args = {**BASE, **kw}
    return infer_reasons(video=video, audio=audio, subtitle=subtitle, has_transcode_session=ts, **args)


@pytest.mark.parametrize("case, kw, expected", [
    ("direct play", {}, []),
    ("subtitle burn only", dict(video="transcode", subtitle="burn", ts=True), ["subtitle_burn"]),
    ("codec change", dict(video="transcode", ts=True, dst_video_codec="hevc"), ["video_codec"]),
    ("downscale", dict(video="transcode", ts=True, dst_resolution="720"), ["video_resolution"]),
    ("bitrate only", dict(video="transcode", ts=True, dst_bitrate_kbps=4000), ["video_bitrate"]),
    ("hdr tonemap", dict(video="transcode", ts=True, src_dynamic_range="HDR10", src_video_codec="hevc",
                         src_resolution="4k", dst_video_codec="hevc"), ["video_resolution", "hdr_tonemap"]),
    ("audio codec", dict(audio="transcode", ts=True, dst_audio_codec="aac"), ["audio_codec"]),
    ("audio downmix", dict(audio="transcode", ts=True, dst_audio_channels=2), ["audio_channels"]),
    ("container remux", dict(video="copy", audio="copy", ts=True, dst_container="mpegts"), ["container"]),
    ("slow transcoder", dict(video="transcode", ts=True, dst_video_codec="hevc", speed=0.8), ["video_codec", "transcoder_slow"]),
    ("throttled is fine", dict(video="transcode", ts=True, dst_video_codec="hevc", speed=0.5, throttled=True), ["video_codec"]),
])
def test_reason_table(case, kw, expected):
    assert reasons(**kw) == expected, case


def test_overall_decision():
    assert overall_decision("direct play", "direct play", None, False) == "direct_play"
    assert overall_decision("copy", "copy", None, True) == "direct_stream"
    assert overall_decision("copy", "transcode", None, True) == "transcode"
    assert overall_decision("copy", "copy", "burn", True) == "transcode"


def test_normalise_decision():
    assert normalise_decision("directplay") == "direct play"
    assert normalise_decision("copy") == "copy"
    assert normalise_decision("") is None


@pytest.mark.parametrize("w, h, label", [(1920, 1080, "1080"), (1920, 800, "1080"), (3840, 1600, "4k"),
                                         (1280, 720, "720"), (720, 576, "576"), (640, 360, "sd")])
def test_resolution_from_size(w, h, label):
    assert resolution_from_size(w, h) == label
