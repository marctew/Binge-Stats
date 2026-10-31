"""SPEC.md section 5, one test per rule."""
import json

import pytest

from bingestats.services import settings
from bingestats.worker.poller import Poller


@pytest.fixture
def events():
    return []


@pytest.fixture
def poller(conn, plex, clock, events):
    return Poller(conn, plex, clock=clock, on_event=lambda t, c: events.append(t))


def plays(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM plays ORDER BY id")]


def active(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM active_sessions")]


def run(poller, clock, seconds=5, times=1):
    for _ in range(times):
        clock.advance(seconds)
        poller.poll_once()


def stop_and_drain(poller, plex, clock):
    plex.stop_all()
    run(poller, clock, times=3)  # grace_polls = 3


def test_new_session_is_recorded_and_user_created(conn, poller, plex, clock, events):
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    [a] = active(conn)
    assert a["state"] == "playing" and a["started_at"] == clock.t
    assert a["start_offset_s"] == 2702
    user = conn.execute("SELECT * FROM users WHERE id = 1001").fetchone()
    assert user["username"] == "alice" and user["share_salt"]
    assert events == ["play_start"]
    assert conn.execute("SELECT name FROM servers").fetchone()["name"] == "Test Plex"


def test_stop_after_grace_polls_uses_last_seen(conn, poller, plex, clock):
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    start = clock.t
    run(poller, clock, 60)               # still playing a minute later
    last_seen = clock.t
    plex.stop_all()
    run(poller, clock, times=2)
    assert active(conn) and not plays(conn)   # within grace
    run(poller, clock)
    [p] = plays(conn)
    assert not active(conn)
    assert p["started_at"] == start and p["stopped_at"] == last_seen
    assert p["play_s"] == 60
    assert p["decision"] == "direct_play" and p["guid"].startswith("plex://episode/")
    assert p["thumb"] == "/library/metadata/62949/thumb/1788494594"


def test_pause_time_is_excluded(conn, poller, plex, clock, events):
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    run(poller, clock, 100)
    plex.meta()["Player"]["state"] = "paused"
    run(poller, clock, 10)
    run(poller, clock, 290)               # paused for 290 s and counting
    plex.meta()["Player"]["state"] = "playing"
    run(poller, clock, 10)
    run(poller, clock, 50)
    stop_and_drain(poller, plex, clock)
    [p] = plays(conn)
    assert p["paused_s"] == 300
    assert p["play_s"] == 460 - 300
    assert "pause" in events and "resume" in events


def test_paused_then_gone_counts_pause_until_last_seen(conn, poller, plex, clock):
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    run(poller, clock, 100)
    plex.meta()["Player"]["state"] = "paused"
    run(poller, clock, 10)                 # paused at t0+110
    run(poller, clock, 200)                # last seen at t0+310, still paused
    stop_and_drain(poller, plex, clock)
    [p] = plays(conn)
    assert p["paused_s"] == 200
    assert p["play_s"] == 110


def test_buffering_counts_and_fires_after_three(conn, poller, plex, clock, events):
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    for _ in range(3):
        plex.meta()["Player"]["state"] = "buffering"
        run(poller, clock, 10)
        plex.meta()["Player"]["state"] = "playing"
        run(poller, clock, 10)
    assert active(conn)[0]["buffer_count"] == 3
    assert events.count("buffer") == 1


def test_short_plays_are_dropped(conn, poller, plex, clock, events):
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    run(poller, clock, 20)                 # 20 s < 30 s minimum
    stop_and_drain(poller, plex, clock)
    assert plays(conn) == [] and active(conn) == []
    assert "play_stop" not in events


def test_keep_history_off_drops_plays(conn, poller, plex, clock):
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    conn.execute("UPDATE users SET keep_history = 0 WHERE id = 1001")
    run(poller, clock, 120)
    stop_and_drain(poller, plex, clock)
    assert plays(conn) == []


def test_watched_threshold_and_event(conn, poller, plex, clock, events):
    plex.play("sessions_direct_play.json")    # 2702 / 2805 = 96 %
    poller.poll_once()
    run(poller, clock, 60)
    run(poller, clock, 60)
    assert events.count("watched") == 1
    stop_and_drain(poller, plex, clock)
    [p] = plays(conn)
    assert p["percent_complete"] == 96 and p["watched"] == 1


def test_not_watched_below_threshold(conn, poller, plex, clock):
    settings.set(conn, "ingest.watched_pct.episode", 99)
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    run(poller, clock, 60)
    stop_and_drain(poller, plex, clock)
    assert plays(conn)[0]["watched"] == 0


def test_item_change_on_same_session_splits_plays(conn, poller, plex, clock, events):
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    run(poller, clock, 120)
    m = plex.meta()
    m["ratingKey"] = "111233"
    m["guid"] = "plex://episode/next"
    m["index"] = 3
    m["viewOffset"] = 0
    run(poller, clock, 5)
    [first] = plays(conn)
    assert first["rating_key"] == 111232 and first["play_s"] == 120
    [a] = active(conn)
    assert a["rating_key"] == 111233 and a["start_offset_s"] == 0
    assert events.count("play_start") == 2


def test_server_down_does_not_end_sessions(conn, poller, plex, clock, events):
    plex.play("sessions_direct_play.json")
    poller.poll_once()
    plex.fail = True
    run(poller, clock, times=10)
    assert active(conn) and active(conn)[0]["missed_polls"] == 0
    assert conn.execute("SELECT is_up FROM servers").fetchone()["is_up"] == 0
    assert events.count("server_down") == 1
    plex.fail = False
    run(poller, clock)
    assert events.count("server_up") == 1
    assert conn.execute("SELECT is_up FROM servers").fetchone()["is_up"] == 1
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM alerts ORDER BY id")]
    assert kinds == ["server_down", "server_up"]


def test_resume_is_grouped(conn, poller, plex, clock):
    plex.play("sessions_direct_play.json")
    plex.meta()["viewOffset"] = 600_000
    poller.poll_once()
    run(poller, clock, 300)
    plex.meta()["viewOffset"] = 900_000
    run(poller, clock, 5)
    stop_and_drain(poller, plex, clock)
    clock.advance(3600)                     # back an hour later, new session, same spot
    plex.play("sessions_direct_play.json")
    m = plex.meta()
    m["Session"]["id"] = "second-session"
    m["viewOffset"] = 905_000
    poller.poll_once()
    run(poller, clock, 120)
    stop_and_drain(poller, plex, clock)
    first, second = plays(conn)
    assert first["group_id"] is None
    assert second["group_id"] == first["id"]


def test_resume_from_different_spot_is_not_grouped(conn, poller, plex, clock):
    plex.play("sessions_direct_play.json")
    plex.meta()["viewOffset"] = 600_000
    poller.poll_once()
    run(poller, clock, 300)
    stop_and_drain(poller, plex, clock)
    plex.play("sessions_direct_play.json")
    m = plex.meta()
    m["Session"]["id"] = "second-session"
    m["viewOffset"] = 60_000                 # started over
    poller.poll_once()
    run(poller, clock, 120)
    stop_and_drain(poller, plex, clock)
    assert [p["group_id"] for p in plays(conn)] == [None, None]


def test_transcode_looks_up_source_once(conn, poller, plex, clock, events):
    plex.play("sessions_transcode.json")
    poller.poll_once()
    run(poller, clock, 60, times=3)
    assert plex.metadata_calls == 1
    assert "transcode_start" in events
    stop_and_drain(poller, plex, clock)
    [p] = plays(conn)
    assert p["decision"] == "transcode"
    assert p["src_resolution"] == "4k" and p["dst_resolution"] == "1080"
    assert p["src_dynamic_range"] == "HDR10"
    assert set(json.loads(p["transcode_reasons"])) >= {"subtitle_burn", "hdr_tonemap", "audio_channels"}
    assert p["hw_decode"] == 1 and p["is_local"] == 0


def test_source_lookup_gives_up_after_three_attempts(conn, poller, plex, clock):
    plex.metadata_data = {}
    plex.play("sessions_transcode.json")
    poller.poll_once()
    run(poller, clock, times=6)
    assert plex.metadata_calls == 3
    assert active(conn)


def test_two_sessions_tracked_independently(conn, poller, plex, clock):
    plex.play("sessions_direct_play.json", "sessions_transcode.json")
    poller.poll_once()
    assert len(active(conn)) == 2
    run(poller, clock, 60)
    plex.play("sessions_transcode.json")
    run(poller, clock, 60, times=3)
    assert len(plays(conn)) == 1 and len(active(conn)) == 1


def test_restart_reconciles_leftover_sessions(conn, plex, clock):
    plex.play("sessions_direct_play.json")
    p1 = Poller(conn, plex, clock=clock)
    p1.poll_once()
    clock.advance(120)
    p1.poll_once()
    # worker restarts; the stream ended while it was down
    plex.stop_all()
    clock.advance(600)
    p2 = Poller(conn, plex, clock=clock)
    for _ in range(3):
        clock.advance(5)
        p2.poll_once()
    [p] = plays(conn)
    assert p["play_s"] == 120


def test_broken_event_sink_does_not_stop_ingest(conn, plex, clock):
    def boom(t, c):
        raise RuntimeError("sink broke")
    p = Poller(conn, plex, clock=clock, on_event=boom)
    plex.play("sessions_direct_play.json")
    p.poll_once()
    assert active(conn)


def test_owner_user_1_maps_to_real_account(conn, poller, plex, clock):
    conn.execute("INSERT INTO users (id, username, share_salt, is_owner) VALUES (5342218, 'marc', 's', 1)")
    plex.play("sessions_direct_play.json")
    plex.meta()["User"]["id"] = "1"
    poller.poll_once()
    assert active(conn)[0]["user_id"] == 5342218
    assert conn.execute("SELECT 1 FROM users WHERE id = 1").fetchone() is None


def test_user_1_kept_until_owner_known(conn, poller, plex, clock):
    plex.play("sessions_direct_play.json")
    plex.meta()["User"]["id"] = "1"
    poller.poll_once()
    assert active(conn)[0]["user_id"] == 1
