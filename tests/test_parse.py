from conftest import load_fixture

from bingestats.plex.models import PlexSession
from bingestats.plex.parse import build_stream, parse_identity, parse_sessions, parse_source_media


def test_direct_play_session_from_live_shape():
    [(s, _raw)] = parse_sessions(load_fixture("sessions_direct_play.json"))
    assert s.session_id == "ugzpro0u9qbnnc0he8th2aw7"
    assert s.user_id == 1001 and s.user_title == "alice"
    assert s.media_type == "episode"
    assert (s.grandparent_title, s.season_index, s.episode_index) == ("The Jury: Murder Trial", 3, 2)
    assert s.guid.startswith("plex://episode/") and s.grandparent_guid.startswith("plex://show/")
    assert s.duration_s == 2805 and s.view_offset_s == 2702
    assert s.library_id == 5
    assert s.is_local and s.ip_address == "192.168.1.50"
    assert s.bandwidth_kbps == 5539
    assert s.poster == "/library/metadata/62949/thumb/1788494594"  # show poster for episodes
    st = s.stream
    assert st.decision == "direct_play"
    assert st.transcode_reasons == []
    assert st.src.video_codec == "h264" and st.src.resolution == "1080" and st.src.dynamic_range == "SDR"
    assert st.dst_resolution == "1080"


def test_transcode_without_source_lookup_keeps_unknowns():
    [(s, _raw)] = parse_sessions(load_fixture("sessions_transcode.json"))
    st = s.stream
    assert st.decision == "transcode"
    assert st.hw_decode and st.hw_encode
    assert st.src.video_codec == "hevc"          # from TranscodeSession
    assert st.src.resolution is None             # Media describes the output, so unknown
    assert not s.is_local and s.ip_address == "203.0.113.20"
    assert "subtitle_burn" in st.transcode_reasons and "video_codec" in st.transcode_reasons


def test_transcode_with_source_lookup_infers_reasons():
    data = load_fixture("sessions_transcode.json")
    raw = data["MediaContainer"]["Metadata"][0]
    src = parse_source_media(load_fixture("metadata_5001.json"), "7001")
    assert src.resolution == "4k" and src.dynamic_range == "HDR10" and src.audio_channels == 8
    st = build_stream(raw, src)
    assert st.src_from_library
    assert set(st.transcode_reasons) == {"subtitle_burn", "video_codec", "video_resolution",
                                         "hdr_tonemap", "audio_codec", "audio_channels"}
    assert st.dst_resolution == "1080" and st.dst_audio_channels == 2


def test_session_roundtrips_through_dict():
    [(s, _raw)] = parse_sessions(load_fixture("sessions_transcode.json"))
    again = PlexSession.from_dict(s.to_dict())
    assert again == s


def test_sessions_without_id_or_user_are_skipped():
    data = load_fixture("sessions_direct_play.json")
    meta = data["MediaContainer"]["Metadata"][0]
    del meta["Session"]
    assert parse_sessions(data) == []


def test_identity():
    ident = parse_identity(load_fixture("identity.json"), load_fixture("root.json"))
    assert ident.machine_id == "ec752ed0testmachine"
    assert ident.name == "Test Plex"
