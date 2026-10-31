"""Importer against a small tautulli.db built from the real schema."""
import json
import sqlite3
from pathlib import Path

import pytest

from bingestats.importers import tautulli
from bingestats.services import settings

SCHEMA = (Path(__file__).parent / "fixtures" / "tautulli_schema.sql").read_text()
T0 = 1_750_000_000


def make_tautulli(path: Path, plays: list[dict], users=None) -> Path:
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for u in users or [
        dict(user_id=1001, username="sarah_p", friendly_name="Sarah", is_admin=0, keep_history=1, deleted_user=0, is_home_user=1),
        dict(user_id=2002, username="dad", friendly_name="dad", is_admin=1, keep_history=1, deleted_user=0, is_home_user=0),
        dict(user_id=3003, username="gone", friendly_name="Old Friend", is_admin=0, keep_history=1, deleted_user=1, is_home_user=0),
        dict(user_id=4004, username="private", friendly_name="", is_admin=0, keep_history=0, deleted_user=0, is_home_user=0),
    ]:
        c.execute("INSERT INTO users (user_id, username, friendly_name, thumb, email, is_admin, keep_history, "
                  "deleted_user, is_home_user) VALUES (:user_id, :username, :friendly_name, "
                  "'https://plex.tv/users/x/avatar', NULL, :is_admin, :keep_history, :deleted_user, :is_home_user)", u)
    for p in plays:
        c.execute("INSERT INTO session_history (id, reference_id, started, stopped, rating_key, user_id, user, "
                  "ip_address, paused_counter, player, product, platform, machine_id, bandwidth, location, secure, "
                  "relayed, parent_rating_key, grandparent_rating_key, media_type, section_id, view_offset, quality_profile) "
                  "VALUES (:id, :ref, :started, :stopped, :rk, :uid, 'x', '192.168.1.5', :paused, 'Living Room', "
                  "'Plex for LG', 'webOS', 'm1', 8000, :loc, 1, 0, :prk, :grk, :mt, :section, :offset, 'Original')", p)
        c.execute("INSERT INTO session_history_metadata (id, rating_key, parent_rating_key, grandparent_rating_key, "
                  "title, parent_title, grandparent_title, media_index, parent_media_index, thumb, parent_thumb, "
                  "grandparent_thumb, media_type, year, duration, guid) VALUES (:id, :rk, :prk, :grk, :title, "
                  ":ptitle, :gtitle, :ep, :season, '/library/metadata/1/thumb/1', '/library/metadata/2/thumb/1', "
                  "'/library/metadata/3/thumb/1', :mt, 2024, :duration, :guid)", p)
        c.execute("INSERT INTO session_history_media_info (id, rating_key, video_decision, audio_decision, "
                  "transcode_decision, duration, container, bitrate, video_codec, video_resolution, "
                  "video_dynamic_range, audio_codec, audio_channels, stream_container, stream_bitrate, "
                  "stream_video_codec, stream_video_resolution, stream_audio_codec, stream_audio_channels, "
                  "stream_subtitle_decision, transcode_hw_decoding, transcode_hw_encoding, transcode_hw_full_pipeline) "
                  "VALUES (:id, :rk, :vd, :ad, :td, :duration, 'mkv', 20000, :vc, :vr, :dr, 'eac3', 6, :sc, 8000, "
                  ":svc, :svr, :sac, :sach, :sub, :hw, :hw, 0)", p)
    c.commit()
    c.close()
    return path


def play(id, **kw):
    base = dict(id=id, ref=id, started=T0 + id * 10_000, paused=60, rk=500 + id, uid=1001, loc="lan", prk=40, grk=30,
                mt="episode", section=5, offset=2_400_000, title=f"Episode {id}", ptitle="Season 1", gtitle="Slow Horses",
                ep=id, season=1, duration=2_700_000, guid=f"plex://episode/{id}", vd="direct play", ad="direct play",
                td="direct play", vc="h264", vr="1080", dr="SDR", sc="mkv", svc="h264", svr="1080", sac="eac3",
                sach=6, sub="", hw=0)
    base.update(kw)
    base.setdefault("stopped", base["started"] + 2_500)
    if "stopped" not in kw:
        base["stopped"] = base["started"] + 2_500
    return base


@pytest.fixture
def server(conn):
    conn.execute("INSERT INTO servers (machine_id, name, is_up) VALUES ('m', 'Plex', 1)")
    return conn


def test_inspect_counts_without_writing(conn, tmp_path):
    path = make_tautulli(tmp_path / "t.db", [play(1), play(2, mt="movie", grk=None, prk=None), play(3, uid=9999)])
    r = tautulli.inspect(conn, path, tmp_path / "work")
    assert r["plays"] == 3 and r["to_import"] == 3 and r["already_imported"] == 0
    assert r["by_type"] == {"episode": 2, "movie": 1}
    assert r["missing_users"] == 1
    assert {u["name"] for u in r["users"]} >= {"Sarah", "dad"}
    assert r["plex_connected"] is False
    assert conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == 0
    assert list((tmp_path / "work").iterdir()) == []   # snapshot cleaned up


def test_import_maps_fields_and_units(server, tmp_path):
    path = make_tautulli(tmp_path / "t.db", [play(1)])
    r = tautulli.run(server, path, tmp_path / "work")
    assert r["imported"] == 1 and r["users_added"] == 4
    p = dict(server.execute("SELECT * FROM plays").fetchone())
    assert p["source"] == "tautulli" and p["source_id"] == 1
    assert p["play_s"] == 2500 - 60 and p["paused_s"] == 60
    assert p["duration_s"] == 2700 and p["view_offset_s"] == 2400
    assert p["percent_complete"] == 89 and p["watched"] == 1
    assert p["decision"] == "direct_play" and json.loads(p["transcode_reasons"]) == []
    assert (p["season_index"], p["episode_index"]) == (1, 1)
    assert p["thumb"] == "/library/metadata/3/thumb/1"     # show poster for episodes
    assert p["is_local"] == 1 and p["product"] == "Plex for LG"
    assert p["src_audio_channels"] == 6


def test_users_mapped(server, tmp_path):
    tautulli.run(server, make_tautulli(tmp_path / "t.db", [play(1)]), tmp_path / "w")
    users = {r["id"]: dict(r) for r in server.execute("SELECT * FROM users")}
    assert users[1001]["friendly_name"] == "Sarah" and users[1001]["is_home"] == 1
    assert users[2002]["friendly_name"] is None          # same as username, so not stored
    assert users[2002]["is_owner"] == 1
    assert users[3003]["is_active"] == 0
    assert users[4004]["keep_history"] == 0
    assert users[1001]["first_seen_at"] == T0 + 10_000


def test_existing_user_details_are_not_overwritten(server, tmp_path):
    server.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (1001, 'sarah_p', 'Sazzle', 's')")
    tautulli.run(server, make_tautulli(tmp_path / "t.db", [play(1)]), tmp_path / "w")
    assert server.execute("SELECT friendly_name FROM users WHERE id = 1001").fetchone()[0] == "Sazzle"


def test_import_is_idempotent(server, tmp_path):
    path = make_tautulli(tmp_path / "t.db", [play(1), play(2)])
    first = tautulli.run(server, path, tmp_path / "w")
    second = tautulli.run(server, path, tmp_path / "w")
    assert first["imported"] == 2
    assert second["imported"] == 0 and second["skipped_existing"] == 2
    assert server.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == 2
    assert tautulli.inspect(server, path, tmp_path / "w")["to_import"] == 0


def test_resume_groups_follow_reference_id(server, tmp_path):
    path = make_tautulli(tmp_path / "t.db", [play(1), play(2, ref=1), play(3, ref=1), play(4)])
    r = tautulli.run(server, path, tmp_path / "w")
    rows = {row["source_id"]: dict(row) for row in server.execute("SELECT * FROM plays")}
    head = rows[1]["id"]
    assert rows[1]["group_id"] is None
    assert rows[2]["group_id"] == head and rows[3]["group_id"] == head
    assert rows[4]["group_id"] is None
    assert r["groups_linked"] == 2


def test_skips_users_with_history_off_and_unknown_users(server, tmp_path):
    path = make_tautulli(tmp_path / "t.db", [play(1, uid=4004), play(2, uid=9999), play(3)])
    r = tautulli.run(server, path, tmp_path / "w")
    assert r["imported"] == 1 and r["skipped_keep_history"] == 1 and r["skipped_no_user"] == 1


def test_overlap_with_live_play_is_skipped(server, tmp_path):
    server.execute("INSERT INTO users (id, username, share_salt) VALUES (1001, 'sarah_p', 's')")
    p = play(1)
    server.execute("INSERT INTO plays (server_id, source, user_id, started_at, stopped_at, play_s, media_type, rating_key) "
                   "VALUES (1, 'live', 1001, ?, ?, 100, 'episode', ?)", (p["started"] + 20, p["stopped"], p["rk"]))
    r = tautulli.run(server, make_tautulli(tmp_path / "t.db", [p, play(2)]), tmp_path / "w")
    assert r["skipped_overlap"] == 1 and r["imported"] == 1


def test_transcode_decisions_and_reasons(server, tmp_path):
    plays = [
        play(1, mt="movie", grk=None, prk=None, td="transcode", vd="transcode", ad="transcode", vc="hevc", vr="4k",
             dr="HDR10", svc="h264", svr="1080", sac="aac", sach=2, hw=1),
        play(2, td="copy", vd="copy", ad="copy", sc="mp4"),
        play(3, td="transcode", vd="transcode", ad="copy", sub="burn"),
        play(4, sub="ignore"),
    ]
    tautulli.run(server, make_tautulli(tmp_path / "t.db", plays), tmp_path / "w")
    rows = {r["source_id"]: dict(r) for r in server.execute("SELECT * FROM plays")}
    assert rows[1]["decision"] == "transcode" and rows[1]["hw_decode"] == 1
    assert set(json.loads(rows[1]["transcode_reasons"])) == {"video_codec", "video_resolution", "hdr_tonemap",
                                                             "audio_codec", "audio_channels"}
    assert rows[1]["src_resolution"] == "4k" and rows[1]["dst_resolution"] == "1080"
    assert rows[2]["decision"] == "direct_stream" and json.loads(rows[2]["transcode_reasons"]) == ["container"]
    assert rows[3]["decision"] == "transcode" and "subtitle_burn" in json.loads(rows[3]["transcode_reasons"])
    assert rows[4]["subtitle_decision"] is None


def test_watched_threshold_from_settings(server, tmp_path):
    settings.set(server, "ingest.watched_pct.episode", 95)
    tautulli.run(server, make_tautulli(tmp_path / "t.db", [play(1)]), tmp_path / "w")
    assert server.execute("SELECT watched FROM plays").fetchone()[0] == 0


def test_show_guid_backfilled_from_library(server, tmp_path):
    server.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title) VALUES (30, 1, 'show', 'plex://show/sh', 'Slow Horses')")
    r = tautulli.run(server, make_tautulli(tmp_path / "t.db", [play(1)]), tmp_path / "w")
    assert server.execute("SELECT grandparent_guid FROM plays").fetchone()[0] == "plex://show/sh"
    assert r["missing_show_guid"] == 0


def test_legacy_guid_upgraded_when_item_still_in_library(server, tmp_path):
    server.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title) VALUES (502, 1, 'episode', 'plex://episode/new', 'E2')")
    tautulli.run(server, make_tautulli(tmp_path / "t.db", [play(2, guid="com.plexapp.agents.thetvdb://1/1/2?lang=en")]), tmp_path / "w")
    assert server.execute("SELECT guid FROM plays").fetchone()[0] == "plex://episode/new"


def test_needs_plex_connected(conn, tmp_path):
    with pytest.raises(tautulli.TautulliImportError, match="Connect Plex"):
        tautulli.run(conn, make_tautulli(tmp_path / "t.db", [play(1)]), tmp_path / "w")


def test_missing_file_and_wrong_schema(server, tmp_path):
    with pytest.raises(tautulli.TautulliImportError, match="No file"):
        tautulli.inspect(server, tmp_path / "nope.db", tmp_path / "w")
    bad = tmp_path / "bad.db"
    sqlite3.connect(bad).execute("CREATE TABLE x (y)").connection.commit()
    with pytest.raises(tautulli.TautulliImportError, match="Missing"):
        tautulli.inspect(server, bad, tmp_path / "w")


def test_bad_row_is_reported_not_fatal(server, tmp_path):
    path = make_tautulli(tmp_path / "t.db", [play(1), play(2)])
    c = sqlite3.connect(path)
    c.execute("UPDATE session_history SET started = 'garbage' WHERE id = 2")
    c.commit()
    r = tautulli.run(server, path, tmp_path / "w")
    assert r["imported"] == 1 and r["failed"] == 1 and r["errors"]
