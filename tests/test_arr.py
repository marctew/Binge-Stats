"""Sonarr, Radarr and SABnzbd: read-only clients, sync, the pages that use them."""
import json

import pytest
import responses

from bingestats import db
from bingestats.arr import parse
from bingestats.arr.client import ArrClient, ArrError, SabClient
from bingestats.plex.parse import external_ids, parse_library_item
from bingestats.services import arr, jobs, settings
from bingestats.worker import arr_sync, jobs_runner

NOW = 1_793_500_000          # 2026-11-01-ish
SON, RAD, SAB = "http://sonarr.test:8989", "http://radarr.test:7878", "http://sab.test:7777"


def series_json(i, title, tvdb, *, ended, files, episodes, seasons):
    return {"id": i, "title": title, "titleSlug": title.lower().replace(" ", "-"), "year": 2010, "tvdbId": tvdb, "imdbId": f"tt{i}", "status": "ended" if ended else "continuing",
            "ended": ended, "monitored": True, "added": "2024-01-01T00:00:00Z", "previousAiring": "2026-10-01T20:00:00Z",
            "statistics": {"episodeFileCount": files, "episodeCount": episodes, "totalEpisodeCount": episodes, "sizeOnDisk": files * 10**9},
            "seasons": [{"seasonNumber": n, "monitored": True,
                         "statistics": {"episodeFileCount": f, "episodeCount": e, "totalEpisodeCount": e}} for n, f, e in seasons]}


def ep(series_id, s, e, *, title="Ep", air="2026-01-01T20:00:00Z", has_file=False, file_quality=None, i=None):
    d = {"id": i or series_id * 1000 + s * 100 + e, "seriesId": series_id, "seasonNumber": s, "episodeNumber": e,
         "title": title, "airDateUtc": air, "hasFile": has_file, "monitored": True}
    if file_quality:
        d["episodeFile"] = {"quality": {"quality": {"name": file_quality}}}
    return d


def movie_json(i, title, tmdb, *, has_file=True, monitored=True, available=True, cutoff=False, collection=None, year=2000):
    m = {"id": i, "title": title, "year": year, "tmdbId": tmdb, "imdbId": f"tt9{i}", "monitored": monitored, "hasFile": has_file,
         "isAvailable": available, "status": "released", "sizeOnDisk": 5 * 10**9 if has_file else 0, "added": "2024-01-01T00:00:00Z"}
    if has_file:
        m["movieFile"] = {"dateAdded": "2024-02-01T00:00:00Z", "qualityCutoffNotMet": cutoff,
                          "quality": {"quality": {"name": "DVD" if cutoff else "Bluray-1080p"}}}
    if collection:
        m["collection"] = {"title": collection[1], "tmdbId": collection[0]}
    return m


def mock_all(rsps, *, sab_fail=False):
    rsps.add(responses.GET, f"{SON}/api/v3/system/status", json={"version": "4.0.19"})
    rsps.add(responses.GET, f"{SON}/api/v3/series", json=[
        series_json(1, "Nearly There", 111, ended=True, files=9, episodes=10, seasons=[(1, 5, 5), (2, 4, 5)]),
        series_json(2, "Way Behind", 222, ended=False, files=2, episodes=12, seasons=[(1, 2, 6), (2, 0, 6)]),
        series_json(3, "All Done", 333, ended=True, files=8, episodes=8, seasons=[(1, 8, 8)]),
        series_json(4, "Keeping Up", 444, ended=False, files=3, episodes=3, seasons=[(1, 3, 3)]),
    ])
    missing = [ep(1, 2, 3)] + [ep(2, 1, e) for e in (3, 4, 5, 6)] + [ep(2, 2, e) for e in range(1, 7)]
    rsps.add(responses.GET, f"{SON}/api/v3/wanted/missing", json={"page": 1, "totalRecords": len(missing), "records": missing})
    rsps.add(responses.GET, f"{SON}/api/v3/wanted/cutoff", json={"page": 1, "totalRecords": 2, "records": [
        ep(3, 1, 1, has_file=True, file_quality="SDTV"), ep(3, 1, 2, has_file=True, file_quality="SDTV")]})
    rsps.add(responses.GET, f"{SON}/api/v3/calendar", json=[
        ep(4, 1, 4, title="Next One", air="2026-11-02T21:00:00Z"), ep(2, 3, 1, title="TBA", air="2026-11-04T20:00:00Z", has_file=True)])
    rsps.add(responses.GET, f"{RAD}/api/v3/system/status", json={"version": "6.3.0"})
    rsps.add(responses.GET, f"{RAD}/api/v3/movie", json=[
        movie_json(1, "Quiet One", 501, collection=(900, "Quiet Collection")),
        movie_json(2, "Quiet Two", 502, has_file=False, collection=(900, "Quiet Collection"), year=2020),
        movie_json(3, "Grainy", 503, cutoff=True),
        movie_json(4, "Not Out Yet", 504, has_file=False, available=False),
        movie_json(5, "Unwanted", 505, has_file=False, monitored=False),
    ])
    rsps.add(responses.GET, f"{RAD}/api/v3/collection", json=[
        {"title": "Quiet Collection", "tmdbId": 900, "monitored": False, "missingMovies": 1, "movies": [
            {"tmdbId": 501, "title": "Quiet One", "year": 2018}, {"tmdbId": 502, "title": "Quiet Two", "year": 2020},
            {"tmdbId": 599, "title": "Quiet Three", "year": 2026}]},
        {"title": "Solo", "tmdbId": 901, "monitored": False, "movies": [{"tmdbId": 503, "title": "Grainy", "year": 2001}]},
    ])
    if sab_fail:
        rsps.add(responses.GET, f"{SAB}/api", json={"status": False, "error": "API Key Incorrect"})
        return
    def sab(request):
        mode = request.params["mode"]
        body = {
            "version": {"version": "5.1.3"},
            "server_stats": {"day": 1, "week": 1, "month": 1, "total": 9 * 10**12, "servers": {
                "news1": {"daily": {"2026-10-30": 40 * 10**9, "2026-10-31": 10 * 10**9}},
                "news2": {"daily": {"2026-10-31": 5 * 10**9}}}},
            "history": {"history": {"noofslots": 1, "slots": [{"nzo_id": "SABnzbd_nzo_1", "name": "Broken.Thing", "category": "tv",
                                                               "bytes": 123, "status": "Failed", "completed": NOW - 3600,
                                                               "fail_message": "Out of retention"}]}},
            "queue": {"queue": {"status": "Downloading", "paused": False, "noofslots": 2, "kbpersec": "12500.0",
                                "mbleft": "4200.5", "timeleft": "0:05:36", "slots": [{"filename": "Show.S01E01"}, {"filename": "Film.2024"}]}},
        }[mode]
        return (200, {}, json.dumps(body))
    rsps.add_callback(responses.GET, f"{SAB}/api", callback=sab)


def connect(c):
    for app, url in (("sonarr", SON), ("radarr", RAD), ("sabnzbd", SAB)):
        settings.set(c, f"{app}.url", url)
        settings.set(c, f"{app}.api_key", f"{app}-key")


def plex_items(c):
    c.execute("INSERT OR IGNORE INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, tvdb_id, thumb) VALUES "
              "(10, 1, 'show', 'plex://show/a', 'Nearly There', 111, '/library/metadata/10/thumb/1')")
    c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, tmdb_id, thumb) VALUES "
              "(20, 1, 'movie', 'plex://movie/q', 'Quiet One', 501, '/library/metadata/20/thumb/1')")


@pytest.fixture
def synced(conn):
    connect(conn)
    plex_items(conn)
    with responses.RequestsMock() as rsps:
        mock_all(rsps)
        result = arr_sync.run(conn, now=NOW)
    assert result["errors"] == {}
    return conn


# -- clients and parsing ------------------------------------------------------------------

def test_clients_are_read_only():
    assert not hasattr(ArrClient("x", "k"), "post") and not hasattr(ArrClient("x", "k"), "delete")
    with pytest.raises(ArrError, match="read-only"):
        SabClient("x", "k").get("delete")
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{SON}/api/v3/series", status=401)
        with pytest.raises(ArrError, match="API key was refused"):
            ArrClient(SON, "bad").get("series")
        req = rsps.calls[0].request
        assert req.headers["X-Api-Key"] == "bad" and req.method == "GET"


def test_parsing():
    assert parse.ts("2026-10-31T01:00:00Z") == 1793408400 and parse.ts(None) is None
    assert parse.sab_daily({"servers": {"a": {"daily": {"d1": 1, "d2": 2}}, "b": {"daily": {"d1": 3}}}}) == {"d1": 4, "d2": 2}
    assert external_ids([{"id": "imdb://tt1845307"}, {"id": "tmdb://39340"}, {"id": "tvdb://248741"}]) == \
        {"tvdb_id": 248741, "tmdb_id": 39340, "imdb_id": "tt1845307"}
    item = parse_library_item({"ratingKey": "5", "type": "show", "title": "X", "Guid": [{"id": "tvdb://77"}]})
    assert item["tvdb_id"] == 77 and item["tmdb_id"] is None


# -- sync -----------------------------------------------------------------------------------

def test_sync_fills_snapshots(synced):
    c = synced
    assert c.execute("SELECT COUNT(*) FROM sonarr_series").fetchone()[0] == 4
    assert c.execute("SELECT COUNT(*) FROM sonarr_missing").fetchone()[0] == 11
    assert c.execute("SELECT COUNT(*) FROM radarr_movies").fetchone()[0] == 5
    assert c.execute("SELECT SUM(bytes) FROM sab_daily").fetchone()[0] == 55 * 10**9
    st = {a["app"]: a for a in arr.view(c)}
    assert st["sonarr"]["ok"] and st["sonarr"]["version"] == "4.0.19" and st["sabnzbd"]["info"]["queue"]["items"] == 2
    assert arr.ready(c, "radarr")


def test_one_app_failing_doesnt_stop_the_rest(conn):
    connect(conn)
    with responses.RequestsMock() as rsps:
        mock_all(rsps, sab_fail=True)
        result = arr_sync.run(conn, now=NOW)
    assert "sabnzbd" in result["errors"] and "API Key Incorrect" in result["errors"]["sabnzbd"]
    assert set(result["synced"]) == {"sonarr", "radarr"}
    st = {a["app"]: a for a in arr.view(conn)}
    assert not st["sabnzbd"]["ok"] and "Incorrect" in st["sabnzbd"]["error"]


def test_resync_replaces_and_keeps_old_sab_days(synced):
    c = synced
    c.execute("INSERT INTO sab_daily (day, bytes) VALUES ('2024-01-01', 7)")   # older than SAB keeps
    with responses.RequestsMock() as rsps:
        mock_all(rsps)
        arr_sync.run(c, now=NOW + 3600)
    assert c.execute("SELECT COUNT(*) FROM sonarr_series").fetchone()[0] == 4
    assert c.execute("SELECT bytes FROM sab_daily WHERE day = '2024-01-01'").fetchone()[0] == 7


def test_queue_tick(synced, conn):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_all(rsps)
        q = arr_sync.queue_tick(synced, now=NOW + 60)
    assert q["status"] == "Downloading" and q["kbps"] == 12500.0
    settings.set(synced, "sabnzbd.url", "")
    assert arr_sync.queue_tick(synced) is None                              # not set up: nothing to do


def test_job_and_schedule(conn, tmp_path):
    for k in ("backup.last_run_at", "arr.last_sync_at"):
        settings.set(conn, k, 0)
    settings.set(conn, "backup.last_run_at", 10**10)
    assert jobs_runner.schedule_due(conn, now=10**10) == []                 # nothing set up
    connect(conn)
    [jid] = jobs_runner.schedule_due(conn, now=10**10)
    job = jobs.claim_next(conn)
    assert job["kind"] == "arr_sync"

    class Cfg:
        data_dir = tmp_path
    with responses.RequestsMock() as rsps:
        mock_all(rsps)
        jobs_runner.run_job(conn, Cfg, job)
    assert settings.get(conn, "arr.last_sync_at") > 0


# -- what the pages use -----------------------------------------------------------------------

def test_sonarr_gaps_and_summary(synced):
    gaps = arr.sonarr_gaps(synced)
    assert [g["title"] for g in gaps] == ["Nearly There", "Way Behind"]
    near = gaps[0]
    assert near["rating_key"] == 10 and near["ended"] and near["holes"][0]["text"] == "E03"
    behind = gaps[1]
    assert [h["whole"] for h in behind["holes"]] == [False, True]
    s = arr.series_summary(synced)
    assert (s["complete_ended"], s["complete_continuing"], s["behind"], s["missing_episodes"]) == (1, 1, 2, 11)


def test_upcoming(synced):
    days = arr.upcoming(synced, now=NOW)
    assert [d["episodes"][0]["show"] for d in days] == ["Keeping Up", "Way Behind"]
    assert days[1]["episodes"][0]["has_file"]


def test_collections_missing_and_cutoff(synced):
    [col] = arr.collections(synced)                                         # 'Solo' has one film: left out
    assert (col["have"], col["total"], col["pct"]) == (1, 3, 33) and col["thumb"]
    assert [m["title"] for m in col["missing"]] == ["Quiet Two", "Quiet Three"]
    assert [m["title"] for m in arr.missing_films(synced)] == ["Quiet Two"]
    cut = arr.below_cutoff(synced)
    assert [(c["app"], c["title"]) for c in cut] == [("radarr", "Grainy"), ("sonarr", "All Done")]
    assert cut[1]["text"] == "E01–E02" and cut[1]["quality"] == "SDTV"
    r = arr.radarr_summary(synced)
    assert (r["films"], r["have"], r["missing"], r["collections"]) == (5, 2, 1, 1)


def test_downloads(synced):
    d = arr.downloads(synced, now=NOW)
    assert d["all"] == 9 * 10**12 and d["busiest"] == ("2026-10-30", 40 * 10**9)
    assert d["failed"][0]["fail_message"] == "Out of retention" and d["queue"]["items"] == 2
    s = arr.downloads_series(synced, days=0, now=NOW)
    assert s["labels"] == ["Oct 26"] and s["series"][0]["data"] == [55.0]


def test_settings_save(conn):
    changed = arr.save(conn, {"sonarr.url": "http://192.168.4.127:8989/", "sonarr.api_key": "abc"})
    assert changed == ["sonarr"] and settings.get(conn, "sonarr.url") == "http://192.168.4.127:8989"
    arr.save(conn, {"sonarr.url": "http://192.168.4.127:8989", "sonarr.api_key": ""})
    assert settings.get(conn, "sonarr.api_key") == "abc"                      # blank keeps it
    with pytest.raises(ValueError, match="http"):
        arr.save(conn, {"radarr.url": "radarr.local"})
    arr.save(conn, {"clear_sonarr": "on"})
    assert not arr.configured(conn)["sonarr"]


# -- pages -----------------------------------------------------------------------------------

def test_pages(logged_in, app_conn):
    c = app_conn
    body = logged_in.get("/insights/collection").data.decode()
    assert "Film collections" not in body and "Below your cutoff" not in body      # only once connected
    assert "Connect SABnzbd" in logged_in.get("/insights/downloads").data.decode()
    assert "Sonarr, Radarr and SABnzbd" in logged_in.get("/settings").data.decode()
    connect(c)
    plex_items(c)
    with responses.RequestsMock() as rsps:
        mock_all(rsps)
        arr_sync.run(c, now=NOW)
    body = logged_in.get("/insights/collection").data.decode()
    assert "Film collections" in body and "Below your cutoff" in body and "complete and ended" in body
    body = logged_in.get("/insights/collection?list=gaps").data.decode()
    assert "Nearly There" in body and "whole season missing" in body and "Ended" in body
    assert "Quiet Three (2026)" in logged_in.get("/insights/collection?list=collections").data.decode()
    assert "Quiet Two" in logged_in.get("/insights/collection?list=missing_films").data.decode()
    assert "SDTV" in logged_in.get("/insights/collection?list=cutoff").data.decode()
    assert "Collection" in logged_in.get("/insights/collection.csv?list=collections").data.decode()
    body = logged_in.get("/insights/downloads").data.decode()
    assert "Downloading now" in body and "Out of retention" in body
    assert "Airing this week" in logged_in.get("/").data.decode()
    r = logged_in.post("/settings/arr", {"sonarr.url": SON, "action": "sync"})
    assert r.status_code == 302 and "/jobs/" in r.headers["Location"]


def test_api(app, app_conn):
    from bingestats.services import api_tokens
    connect(app_conn)
    with responses.RequestsMock() as rsps:
        mock_all(rsps)
        arr_sync.run(app_conn, now=NOW)
    h = {"Authorization": f"Bearer {api_tokens.create(app_conn, 'x', 'read')}"}
    d = app.test_client().get("/api/v1/insights/arr", headers=h).get_json()
    assert d["series"]["behind"] == 2 and all("api_key" not in json.dumps(a) for a in d["apps"])
    assert "sonarr-key" not in json.dumps(d)
    assert app.test_client().get("/api/v1/insights/downloads/series?days=7", headers=h).get_json()["labels"]


def test_mqtt_download_sensors(synced):
    from bingestats.worker.mqtt import discovery, sab_topics, server_state
    cfg = {"mqtt.base_topic": "bingestats", "mqtt.discovery_prefix": "homeassistant"}
    assert all(t in discovery(cfg, [], "Plex", sab=True) for t in sab_topics(cfg))
    assert not any(t in discovery(cfg, [], "Plex", sab=False) for t in sab_topics(cfg))
    st = server_state(synced, [], NOW)
    assert st["download_items"] == 2 and st["download_mbps"] == 100.0


def test_migration_8(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "v7.db")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:7])
    monkeypatch.setattr(db, "LATEST", 7)
    db.migrate(c)
    c.execute("INSERT INTO media_items (rating_key, server_id, type, title) VALUES (1, 1, 'show', 'Old')")
    monkeypatch.undo()
    assert db.migrate(c) == db.LATEST
    assert c.execute("SELECT tvdb_id FROM media_items").fetchone()[0] is None
    assert c.execute("SELECT COUNT(*) FROM sonarr_series").fetchone()[0] == 0



# -- "open in" links --------------------------------------------------------------------------

def test_item_links(synced):
    c = synced
    c.execute("UPDATE servers SET machine_id = 'abc123' WHERE id = 1")
    c.execute("INSERT INTO media_items (rating_key, server_id, type, title, grandparent_rating_key) "
              "VALUES (11, 1, 'episode', 'E1', 10)")
    show = dict(c.execute("SELECT * FROM media_items WHERE rating_key = 10").fetchone())
    links = {l["app"]: l["url"] for l in arr.item_links(c, show)}
    assert links["plex"] == "https://app.plex.tv/desktop/#!/server/abc123/details?key=%2Flibrary%2Fmetadata%2F10"
    assert links["sonarr"] == f"{SON}/series/nearly-there"
    episode = dict(c.execute("SELECT * FROM media_items WHERE rating_key = 11").fetchone())
    assert {l["app"] for l in arr.item_links(c, episode)} == {"plex", "sonarr"}       # via its show
    film = dict(c.execute("SELECT * FROM media_items WHERE rating_key = 20").fetchone())
    assert {l["app"]: l["url"] for l in arr.item_links(c, film)}["radarr"] == f"{RAD}/movie/501"
    c.execute("INSERT INTO media_items (rating_key, server_id, type, title, tmdb_id) VALUES (21, 1, 'movie', 'Not In Radarr', 777)")
    other = dict(c.execute("SELECT * FROM media_items WHERE rating_key = 21").fetchone())
    assert [l["app"] for l in arr.item_links(c, other)] == ["plex"]
    assert arr.item_links(c, {**film, "is_present": 0}) == [{"app": "radarr", "label": "Open in Radarr", "url": f"{RAD}/movie/501"}]
    arr.save(c, {"clear_radarr": "on"})
    assert [l["app"] for l in arr.item_links(c, film)] == ["plex"]                     # not connected: no button


def test_item_page_buttons(logged_in, app_conn):
    connect(app_conn)
    plex_items(app_conn)
    app_conn.execute("UPDATE servers SET machine_id = 'abc123' WHERE id = 1")
    with responses.RequestsMock() as rsps:
        mock_all(rsps)
        arr_sync.run(app_conn, now=NOW)
    body = logged_in.get("/item/10").data.decode()
    assert "Open in Plex" in body and f"{SON}/series/nearly-there" in body and 'target="_blank"' in body
    assert f"{RAD}/movie/501" in logged_in.get("/item/20").data.decode()


def test_migration_9(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "v8.db")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:8])
    monkeypatch.setattr(db, "LATEST", 8)
    db.migrate(c)
    c.execute("INSERT INTO sonarr_series (id, title) VALUES (1, 'Old')")
    monkeypatch.undo()
    assert db.migrate(c) == db.LATEST
    assert c.execute("SELECT title_slug FROM sonarr_series").fetchone()[0] is None
