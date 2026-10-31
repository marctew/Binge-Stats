import pytest

from bingestats.services import jobs


@pytest.fixture
def seeded(app_conn):
    c = app_conn
    c.execute("INSERT INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'Plex', 1)")
    c.execute("INSERT INTO users (id, username, friendly_name, share_salt, is_home) VALUES (1001, 'sarah_p', 'Sarah', 's', 1)")
    c.execute("INSERT INTO users (id, username, share_salt, is_owner) VALUES (2002, 'marc', 's', 1)")
    c.execute("INSERT INTO users (id, username, share_salt, is_active) VALUES (3003, 'gone', 's', 0)")
    c.execute("INSERT INTO libraries (id, server_id, name, type, item_count, child_count, total_size_bytes, synced_at) "
              "VALUES (5, 1, 'TV Programmes', 'show', 1, 2, 4000000000, 1700000000)")
    c.execute("INSERT INTO media_items (rating_key, server_id, library_id, type, guid, title, added_at, thumb, art) "
              "VALUES (30, 1, 5, 'show', 'plex://show/sh', 'Slow Horses', 1700000000, '/library/metadata/30/thumb/1', "
              "'/library/metadata/30/art/1')")
    for rk, idx in ((501, 1), (502, 2)):
        c.execute("INSERT INTO media_items (rating_key, server_id, library_id, type, guid, title, grandparent_rating_key, "
                  "grandparent_title, season_index, episode_index, size_bytes) VALUES (?, 1, 5, 'episode', ?, ?, 30, "
                  "'Slow Horses', 1, ?, 2000000000)", (rk, f"plex://episode/{rk}", f"Ep {idx}", idx))
    for i, (uid, rk) in enumerate(((1001, 501), (1001, 502), (2002, 501))):
        c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, rating_key, guid, "
                  "grandparent_rating_key, grandparent_guid, grandparent_title, title, season_index, episode_index, "
                  "library_id, watched, decision, player_title, product) VALUES (1, ?, ?, ?, 2500, 'episode', ?, ?, 30, "
                  "'plex://show/sh', 'Slow Horses', ?, 1, ?, 5, 1, 'direct_play', 'Living Room', 'Plex for LG')",
                  (uid, 1_700_000_000 + i * 5000, 1_700_002_500 + i * 5000, rk, f"plex://episode/{rk}", f"Ep {rk - 500}", rk - 500))
    # a film that's gone from the library
    c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, rating_key, title, year, "
              "library_id, decision) VALUES (1, 2002, 1700100000, 1700105000, 5000, 'movie', 777, 'Gone Film', 2019, 4, 'transcode')")
    return c


def test_users_list(logged_in, seeded):
    body = logged_in.get("/users").data.decode()
    assert "Sarah" in body and "marc" in body and "Owner" in body
    assert "gone" not in body
    assert "gone" in logged_in.get("/users?all=1").data.decode()


def test_user_page_and_edit(logged_in, seeded):
    body = logged_in.get("/users/1001").data.decode()
    assert "Sarah" in body and "Slow Horses" in body and "Living Room" in body
    logged_in.post("/users/1001", {"friendly_name": "Saz", "keep_history": "on"})
    row = seeded.execute("SELECT friendly_name, keep_history FROM users WHERE id = 1001").fetchone()
    assert tuple(row) == ("Saz", 1)
    logged_in.post("/users/1001", {"friendly_name": ""})
    row = seeded.execute("SELECT friendly_name, keep_history FROM users WHERE id = 1001").fetchone()
    assert tuple(row) == (None, 0)
    assert logged_in.get("/users/424242").status_code == 404


def test_libraries_pages(logged_in, seeded):
    body = logged_in.get("/libraries").data.decode()
    assert "TV Programmes" in body and "3.7 GB" in body
    body = logged_in.get("/libraries/5").data.decode()
    assert "Slow Horses" in body and "Most played" in body
    assert logged_in.get("/libraries/99").status_code == 404


def test_item_page_show(logged_in, seeded):
    body = logged_in.get("/item/30").data.decode()
    assert "Slow Horses" in body and "2 of 2 episodes watched" in body and "1 of 2 episodes watched" in body


def test_item_page_episode_and_gone_film(logged_in, seeded):
    assert "Ep 1" in logged_in.get("/item/501").data.decode()
    body = logged_in.get("/item/777").data.decode()
    assert "Gone Film" in body and "No longer in Plex" in body
    assert logged_in.get("/item/123456").status_code == 404


def test_history_links_to_show_and_user(logged_in, seeded):
    body = logged_in.get("/history").data.decode()
    assert "/item/30" in body and "/users/1001" in body


def test_import_flow_creates_jobs(logged_in, app_conn):
    assert logged_in.get("/import").status_code == 200
    r = logged_in.post("/import", {"path": "/data/import/tautulli.db", "action": "check"})
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]
    job = jobs.latest(app_conn, "tautulli_check")
    assert job["params"] == {"path": "/data/import/tautulli.db"} and job["status"] == "queued"
    body = logged_in.get(r.headers["Location"]).data.decode()
    assert "Waiting for the worker" in body
    # finished check renders the report with an import button
    jobs.claim_next(app_conn)
    jobs.finish(app_conn, job["id"], {"plays": 10, "to_import": 7, "already_imported": 3, "first_play": 1, "last_play": 2,
                                      "by_type": {"episode": 10}, "users": [{"name": "Sarah", "plays": 10}],
                                      "missing_users": 0, "plex_connected": True})
    body = logged_in.get(f"/jobs/{job['id']}/partial").data.decode()
    assert "Import 7 plays" in body and "hx-trigger" not in body


def test_import_result_render(logged_in, app_conn):
    jid = jobs.create(app_conn, "tautulli_import", {"path": "x"})
    jobs.claim_next(app_conn)
    jobs.finish(app_conn, jid, {"imported": 5545, "skipped_existing": 0, "skipped_overlap": 2, "skipped_no_user": 0,
                                "skipped_keep_history": 0, "failed": 0, "errors": [], "users_added": 14,
                                "groups_linked": 1091, "missing_show_guid": 0})
    body = logged_in.get(f"/jobs/{jid}").data.decode()
    assert "5,545" in body and "1,091" in body and "already recorded them live" in body


def test_settings_sync_buttons(logged_in, app_conn):
    assert "Recent jobs" in logged_in.get("/settings").data.decode()
    r = logged_in.post("/settings/sync", {"kind": "library_sync"})
    assert "/jobs/" in r.headers["Location"]
    r2 = logged_in.post("/settings/sync", {"kind": "library_sync"})
    assert r2.headers["Location"] == r.headers["Location"]           # no duplicate job
    assert jobs.latest(app_conn, "library_sync")["status"] == "queued"


def test_empty_pages_render(logged_in):
    for url in ("/users", "/libraries", "/import"):
        assert logged_in.get(url).status_code == 200, url
