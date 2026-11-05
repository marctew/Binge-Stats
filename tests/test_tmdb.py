"""TMDB: the read-only client, which shows are due, the sync job, settings and the schedule."""
import json

import pytest
import responses

from bingestats import db
from bingestats.services import jobs, pins, settings
from bingestats.services import tmdb as tmdb_svc
from bingestats.tmdb import parse
from bingestats.tmdb.client import BASE, TmdbClient, TmdbError
from bingestats.worker import jobs_runner, tmdb_sync

NOW = 1_793_900_000
KEY = "0123456789abcdef0123456789abcdef"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ4In0.sig"


def tv_json(name, *, status="Returning Series", in_production=True, nxt=None, last_season=2, seasons=(0, 1, 2)):
    return {"name": name, "status": status, "in_production": in_production, "last_air_date": "2026-03-01",
            "next_episode_to_air": nxt, "last_episode_to_air": {"season_number": last_season},
            "seasons": [{"season_number": s} for s in seasons],
            "genres": [{"name": "Drama"}, {"name": "Crime"}], "networks": [{"name": "BBC One"}]}


# -- client ---------------------------------------------------------------------------------

@responses.activate
def test_client_sends_a_key_as_api_key_and_a_token_as_bearer():
    responses.add(responses.GET, f"{BASE}/tv/1", json={"name": "A"})
    TmdbClient(KEY).tv(1)
    TmdbClient(TOKEN).tv(1)
    first, second = responses.calls
    assert f"api_key={KEY}" in first.request.url and "Authorization" not in first.request.headers
    assert second.request.headers["Authorization"] == f"Bearer {TOKEN}" and "api_key" not in second.request.url


@responses.activate
def test_client_404_401_and_429():
    responses.add(responses.GET, f"{BASE}/tv/404", status=404)
    responses.add(responses.GET, f"{BASE}/tv/9", status=429, headers={"Retry-After": "2"})
    responses.add(responses.GET, f"{BASE}/tv/9", json={"name": "Later"})
    responses.add(responses.GET, f"{BASE}/tv/401", status=401)
    waits = []
    c = TmdbClient(KEY, sleep=waits.append)
    assert c.tv(404) is None
    assert c.tv(9) == {"name": "Later"} and waits == [2.0]
    with pytest.raises(TmdbError, match="refused"):
        c.tv(401)


def test_client_has_no_write_methods():
    assert not any(hasattr(TmdbClient, m) for m in ("post", "put", "delete", "patch"))


def test_parse():
    row = parse.tv(tv_json("Silo", nxt={"air_date": "2026-12-01", "season_number": 3}), 5, 50, NOW)
    assert (row["status"], row["in_production"], row["next_air_date"], row["next_season"]) == \
        ("Returning Series", 1, "2026-12-01", 3)
    assert (row["last_season"], row["max_season"]) == (2, 2)          # specials left out of max_season
    assert json.loads(row["genres"]) == ["Drama", "Crime"] and json.loads(row["networks"]) == ["BBC One"]


# -- what's due -----------------------------------------------------------------------------

@pytest.fixture
def shows(conn):
    conn.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    conn.execute("INSERT INTO users (id, username, is_owner, share_salt) VALUES (1, 'marc', 1, 's')")
    for rk, title, tmdb, tvdb in ((10, "Pinned", 100, 1000), (11, "Plex Only", 101, None), (12, "No Ids", None, None)):
        conn.execute("INSERT INTO media_items (rating_key, server_id, type, title, tmdb_id, tvdb_id) "
                     "VALUES (?, 1, 'show', ?, ?, ?)", (rk, title, tmdb, tvdb))
    conn.execute("INSERT INTO sonarr_series (id, title, tvdb_id, tmdb_id) VALUES (1, 'Sonarr Only', 2000, 102)")
    conn.execute("INSERT INTO sonarr_series (id, title, tvdb_id, tmdb_id) VALUES (2, 'Pinned', 1000, 100)")
    pins.pin(conn, 1, 1000)
    settings.set(conn, "tmdb.api_key", KEY)
    return conn


def test_due_reads_everything_first_with_pins_first(shows):
    assert tmdb_svc.due(shows, NOW)[0] == (100, 1000)
    assert sorted(tmdb_svc.due(shows, NOW)) == [(100, 1000), (101, None), (102, 2000)]


def fake_client(fail_at=None, gone=()):
    calls = []

    class C:
        def tv(self, tmdb_id):
            calls.append(tmdb_id)
            if tmdb_id == fail_at:
                raise TmdbError("TMDB refused the key")
            return None if tmdb_id in gone else tv_json(f"Show {tmdb_id}")
    return C(), calls


def test_run_then_only_whats_due(shows):
    c, calls = fake_client(gone={101})
    assert tmdb_sync.run(shows, now=NOW, client=c, sleep=lambda s: None) == {"read": 2, "not_found": 1, "due": 3}
    assert shows.execute("SELECT COUNT(*) FROM tmdb_shows").fetchone()[0] == 3
    assert shows.execute("SELECT tvdb_id, name FROM tmdb_shows WHERE tmdb_id = 100").fetchone()[:] == (1000, "Show 100")
    assert tmdb_svc.due(shows, NOW + 3600) == []
    assert tmdb_svc.due(shows, NOW + 21 * 3600) == [(100, 1000)]               # pinned: daily
    assert sorted(tmdb_svc.due(shows, NOW + 8 * 86400)) == [(100, 1000), (101, None), (102, 2000)]
    v = tmdb_svc.view(shows)
    assert v["ok"] and v["shows"] == 2 and v["known"] == 3


def test_a_failure_keeps_what_was_read_and_says_why(shows):
    c, _ = fake_client(fail_at=102)
    with pytest.raises(TmdbError):
        tmdb_sync.run(shows, now=NOW, client=c, sleep=lambda s: None)
    assert shows.execute("SELECT COUNT(*) FROM tmdb_shows").fetchone()[0] == 2
    v = tmdb_svc.view(shows)
    assert not v["ok"] and v["error"] == "TMDB refused the key"


def test_not_set_up(conn):
    assert tmdb_sync.run(conn, now=NOW) == {"skipped": "TMDB isn't set up"}


def test_schedule(shows):
    settings.set(shows, "backup.last_run_at", NOW)
    assert "tmdb_sync" in [j["kind"] for j in (jobs.get(shows, i) for i in jobs_runner.schedule_due(shows, now=NOW))]
    settings.set(shows, "tmdb.last_run_at", NOW)
    shows.execute("DELETE FROM jobs")
    assert jobs_runner.schedule_due(shows, now=NOW + 3600) == []
    settings.set(shows, "tmdb.api_key", "")
    assert jobs_runner.schedule_due(shows, now=NOW + 7 * 3600) == []


def test_migration_11(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "v10.db")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:10])
    monkeypatch.setattr(db, "LATEST", 10)
    db.migrate(c)
    c.execute("INSERT INTO tmdb_shows (tmdb_id, status) VALUES (1, 'Ended')")
    monkeypatch.undo()
    assert db.migrate(c) == db.LATEST
    assert tuple(c.execute("SELECT status, genres, networks, max_season FROM tmdb_shows").fetchone()) == \
        ("Ended", "[]", "[]", None)


# -- settings --------------------------------------------------------------------------------

def test_save(conn):
    assert tmdb_svc.save(conn, {"tmdb.api_key": KEY}) is True and tmdb_svc.configured(conn)
    assert tmdb_svc.save(conn, {"tmdb.api_key": ""}) is False and settings.get(conn, "tmdb.api_key") == KEY
    with pytest.raises(ValueError):
        tmdb_svc.save(conn, {"tmdb.api_key": "too short"})
    assert tmdb_svc.save(conn, {"clear_tmdb": "on"}) is True and not tmdb_svc.configured(conn)


def test_settings_page(logged_in, app_conn):
    page = logged_in.get("/settings").data.decode()
    assert 'id="tmdb"' in page and "Not set up" in page
    r = logged_in.post("/settings/tmdb", {"tmdb.api_key": KEY})
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]          # saving reads straight away
    page = logged_in.get("/settings").data.decode()
    assert KEY not in page and "Saved – leave blank to keep" in page and "Read now" in page
    r = logged_in.post("/settings/tmdb", {"clear_tmdb": "on"})
    assert r.headers["Location"].endswith("#tmdb") and not tmdb_svc.configured(app_conn)
