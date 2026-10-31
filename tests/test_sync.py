import pytest

from bingestats.services import jobs, settings
from bingestats.worker import jobs_runner
from bingestats.worker.library_sync import sync_libraries, sync_users


def ep(rk, show_rk, title, idx, added=1_700_000_000):
    return {"ratingKey": str(rk), "type": "episode", "guid": f"plex://episode/{rk}", "title": title,
            "grandparentRatingKey": str(show_rk), "grandparentTitle": "Slow Horses", "parentRatingKey": "41",
            "parentTitle": "Season 1", "parentIndex": 1, "index": idx, "duration": 2_700_000, "addedAt": added,
            "thumb": f"/library/metadata/{rk}/thumb/1",
            "Media": [{"videoCodec": "h264", "videoResolution": "1080", "audioCodec": "eac3", "audioChannels": 6,
                       "container": "mkv", "bitrate": 9000, "Part": [{"size": 2_000_000_000}]}]}


@pytest.fixture
def lib_plex(plex):
    plex.sections_list = [{"key": "4", "type": "movie", "title": "Films"},
                          {"key": "5", "type": "show", "title": "TV Programmes"},
                          {"key": "9", "type": "photo", "title": "Photos"}]
    plex.library = {
        (4, 1): [{"ratingKey": "100", "type": "movie", "guid": "plex://movie/a", "title": "Aftersun", "year": 2022,
                  "addedAt": 1_700_000_000, "Media": [{"videoCodec": "hevc", "width": 3840, "height": 1600,
                                                       "Part": [{"size": 30_000_000_000}]}]}],
        (5, 2): [{"ratingKey": "30", "type": "show", "guid": "plex://show/sh", "title": "Slow Horses",
                  "addedAt": 1_700_000_000}],
        (5, 4): [ep(500 + i, 30, f"Ep {i}", i) for i in range(1, 8)],
    }
    return plex


@pytest.fixture
def srv(conn):
    conn.execute("INSERT INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'Plex', 1)")
    return conn


def test_library_sync_stores_items_and_totals(srv, lib_plex, clock):
    r = sync_libraries(srv, lib_plex, 1, clock=clock)
    assert r["libraries"] == 2 and r["items"] == 9            # photo library skipped
    libs = {row["id"]: dict(row) for row in srv.execute("SELECT * FROM libraries")}
    assert libs[5]["item_count"] == 1 and libs[5]["child_count"] == 7
    assert libs[5]["total_size_bytes"] == 14_000_000_000
    assert libs[4]["item_count"] == 1
    film = dict(srv.execute("SELECT * FROM media_items WHERE rating_key = 100").fetchone())
    assert film["video_resolution"] == "4k" and film["size_bytes"] == 30_000_000_000
    e = dict(srv.execute("SELECT * FROM media_items WHERE rating_key = 503").fetchone())
    assert (e["season_index"], e["episode_index"], e["grandparent_rating_key"]) == (1, 3, 30)
    assert settings.get(srv, "library.last_sync_at") == int(clock.t)
    # first sync announces nothing
    assert srv.execute("SELECT COUNT(*) FROM recently_added_queue").fetchone()[0] == 0


def test_library_sync_pages(srv, lib_plex, clock, monkeypatch):
    from bingestats.worker import library_sync
    monkeypatch.setattr(library_sync, "PAGE", 3)
    sync_libraries(srv, lib_plex, 1, clock=clock)
    assert srv.execute("SELECT COUNT(*) FROM media_items WHERE type = 'episode'").fetchone()[0] == 7


def test_resync_marks_removed_and_queues_new(srv, lib_plex, clock):
    sync_libraries(srv, lib_plex, 1, clock=clock)
    lib_plex.library[(5, 4)] = lib_plex.library[(5, 4)][1:] + [ep(600, 30, "New one", 8)]
    clock.advance(3600)
    r = sync_libraries(srv, lib_plex, 1, clock=clock)
    assert r["removed"] == 1 and r["new"] == 1
    assert srv.execute("SELECT is_present FROM media_items WHERE rating_key = 501").fetchone()[0] == 0
    assert [row[0] for row in srv.execute("SELECT rating_key FROM recently_added_queue")] == [600]
    assert srv.execute("SELECT child_count FROM libraries WHERE id = 5").fetchone()[0] == 7


def test_library_sync_backfills_play_guids(srv, lib_plex, clock):
    srv.execute("INSERT INTO users (id, username, share_salt) VALUES (1, 'u', 's')")
    srv.execute("INSERT INTO plays (server_id, source, source_id, user_id, started_at, stopped_at, play_s, media_type, "
                "rating_key, grandparent_rating_key, guid) VALUES (1, 'tautulli', 1, 1, 0, 1, 1, 'episode', 503, 30, "
                "'com.plexapp.agents.thetvdb://x')")
    r = sync_libraries(srv, lib_plex, 1, clock=clock)
    row = srv.execute("SELECT guid, grandparent_guid FROM plays").fetchone()
    assert tuple(row) == ("plex://episode/503", "plex://show/sh")
    assert r["plays_matched"] == 2


USERS_XML = """<?xml version="1.0"?><MediaContainer friendlyName="myPlex">
<User id="10314720" title="farfriend" username="farfriend" email="f@example.com" thumb="https://plex.tv/users/f/avatar" home="0"/>
<User id="438643104" title="Sarah" thumb="https://plex.tv/users/s/avatar" home="1"/>
</MediaContainer>"""


def test_user_sync(srv, plex, clock):
    srv.execute("INSERT INTO users (id, username, friendly_name, share_salt, is_active) VALUES (999, 'removed', 'Old', 's', 1)")
    srv.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (438643104, 'sarah', 'Sazzle', 's')")
    plex.plextv_data = {
        "/api/v2/user": {"id": 5342218, "username": "marc", "email": "m@example.com", "thumb": "https://plex.tv/users/m/avatar", "home": True},
        "/api/v2/home/users": {"users": [
            {"id": 5342218, "username": "marc", "title": "marc", "admin": True},
            {"id": 438643104, "title": "Sarah", "friendlyName": "Sarah P", "admin": False, "thumb": "https://plex.tv/users/s2/avatar"},
            {"id": 26454279, "title": "Guest", "admin": False, "guest": True}]},
        "/api/users": USERS_XML,
    }
    r = sync_users(srv, plex, clock=clock)
    assert r["users"] == 4 and r["added"] == 3 and r["deactivated"] == 1
    u = {row["id"]: dict(row) for row in srv.execute("SELECT * FROM users")}
    assert u[5342218]["is_owner"] == 1 and u[5342218]["is_home"] == 1
    assert u[438643104]["friendly_name"] == "Sazzle"          # admin's name kept
    assert u[438643104]["is_home"] == 1
    assert u[10314720]["is_home"] == 0 and u[10314720]["email"] == "f@example.com"
    assert u[999]["is_active"] == 0
    assert settings.get(srv, "users.last_sync_at") == int(clock.t)


def test_user_sync_without_plex_home(srv, plex, clock):
    plex.plextv_data = {"/api/v2/user": {"id": 1, "username": "solo"}, "/api/users": "<MediaContainer/>"}
    r = sync_users(srv, plex, clock=clock)
    assert r["users"] == 1


# -- jobs ---------------------------------------------------------------------------

def test_jobs_lifecycle(conn):
    jid = jobs.create(conn, "library_sync")
    with pytest.raises(jobs.JobBusy):
        jobs.create(conn, "library_sync")
    assert jobs.ensure(conn, "library_sync") == jid
    job = jobs.claim_next(conn)
    assert job["id"] == jid and job["status"] == "running"
    assert jobs.claim_next(conn) is None
    jobs.progress(conn, jid, 150, "almost")
    assert jobs.get(conn, jid)["progress"] == 100
    jobs.finish(conn, jid, {"items": 3})
    j = jobs.get(conn, jid)
    assert j["status"] == "done" and j["result"] == {"items": 3}
    jobs.create(conn, "library_sync")                        # allowed again once done


def test_orphaned_jobs_fail_on_start(conn):
    jobs.create(conn, "user_sync")
    jobs.claim_next(conn)
    assert jobs.fail_orphans(conn) == 1
    assert jobs.latest(conn, "user_sync")["status"] == "failed"


def test_unknown_job_kind(conn):
    with pytest.raises(ValueError):
        jobs.create(conn, "rm_rf")


def test_schedule_due(srv, clock):
    settings.set(srv, "backup.last_run_at", int(clock.t))           # covered by test_backups
    assert jobs_runner.schedule_due(srv, now=clock.t) == []        # plex not configured
    settings.set(srv, "plex.url", "http://p:32400")
    settings.set(srv, "plex.token", "t")
    settings.set(srv, "geo.enabled", False)
    queued = jobs_runner.schedule_due(srv, now=clock.t)
    assert len(queued) == 2
    assert jobs_runner.schedule_due(srv, now=clock.t) == queued  # not duplicated
    for _ in queued:
        j = jobs.claim_next(srv)
        jobs.finish(srv, j["id"], {})
    settings.set(srv, "library.last_sync_at", int(clock.t))
    settings.set(srv, "users.last_sync_at", int(clock.t))
    assert jobs_runner.schedule_due(srv, now=clock.t + 60) == []
    assert len(jobs_runner.schedule_due(srv, now=clock.t + 7 * 3600)) == 2


def test_run_job_dispatch(srv, lib_plex, monkeypatch, tmp_path):
    from bingestats.services import activity
    monkeypatch.setattr(activity, "plex_client", lambda conn: lib_plex)

    class Cfg:
        data_dir = tmp_path
    jid = jobs.create(srv, "library_sync")
    result = jobs_runner.run_job(srv, Cfg, jobs.claim_next(srv))
    assert result["items"] == 9
    assert jobs.get(srv, jid)["progress"] > 0


def test_user_sync_merges_local_owner(srv, plex, clock):
    srv.execute("INSERT INTO users (id, username, share_salt, thumb_url, first_seen_at) VALUES (1, 'marc', 's', 'https://plex.tv/users/m/avatar', 100)")
    srv.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type) VALUES (1, 1, 0, 1, 1, 'movie')")
    srv.execute("INSERT INTO active_sessions (session_id, server_id, user_id, started_at, last_seen_at, state, state_since, snapshot) "
                "VALUES ('s1', 1, 1, 0, 0, 'playing', 0, '{}')")
    plex.plextv_data = {"/api/v2/user": {"id": 5342218, "username": "marc"}, "/api/users": "<MediaContainer/>"}
    r = sync_users(srv, plex, clock=clock)
    assert r["owner_plays_merged"] == 1
    assert srv.execute("SELECT COUNT(*) FROM plays WHERE user_id = 5342218").fetchone()[0] == 1
    assert srv.execute("SELECT user_id FROM active_sessions").fetchone()[0] == 5342218
    owner = srv.execute("SELECT * FROM users WHERE id = 5342218").fetchone()
    assert owner["thumb_url"] == "https://plex.tv/users/m/avatar" and owner["first_seen_at"] == 100
    assert srv.execute("SELECT 1 FROM users WHERE id = 1").fetchone() is None
