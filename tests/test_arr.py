"""Sonarr, Radarr and SABnzbd: read-only clients, sync, the pages that use them."""
import json

import pytest
import requests
import responses

from bingestats import db
from bingestats.arr import parse
from bingestats.arr.client import ArrClient, ArrError, SabClient
from bingestats.plex.parse import external_ids, parse_library_item
from bingestats.services import api_tokens, arr, jobs, settings
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



def test_links_use_the_public_address(synced):
    c = synced
    c.execute("UPDATE servers SET machine_id = 'abc123' WHERE id = 1")
    arr.save(c, {"sonarr.url": SON, "sonarr.link_url": "https://sonarr.example.co.uk/",
                 "radarr.url": RAD, "radarr.link_url": "https://radarr.example.co.uk"})
    show = dict(c.execute("SELECT * FROM media_items WHERE rating_key = 10").fetchone())
    film = dict(c.execute("SELECT * FROM media_items WHERE rating_key = 20").fetchone())
    assert {l["app"]: l["url"] for l in arr.item_links(c, show)}["sonarr"] == "https://sonarr.example.co.uk/series/nearly-there"
    assert {l["app"]: l["url"] for l in arr.item_links(c, film)}["radarr"] == "https://radarr.example.co.uk/movie/501"
    assert settings.get(c, "sonarr.url") == SON                               # the sync still reads the LAN address
    with pytest.raises(ValueError, match="link address"):
        arr.save(c, {"sonarr.url": SON, "sonarr.link_url": "sonarr.example.co.uk"})



# -- live downloads ---------------------------------------------------------------------------

from bingestats.worker import downloads as dl  # noqa: E402

TV_ID, FILM_ID, HIDDEN_ID, NEW_ID = "SABnzbd_nzo_tv", "SABnzbd_nzo_film", "SABnzbd_nzo_x", "SABnzbd_nzo_new"


def slot(nzo, name, cat, pct=40, mb=1000.0, left=600.0, status="Downloading", tl="0:01:00"):
    return {"nzo_id": nzo, "filename": name, "cat": cat, "status": status, "percentage": str(pct),
            "mb": str(mb), "mbleft": str(left), "timeleft": tl, "priority": "Normal"}


def mock_live(rsps, slots, *, sonarr_calls=None, sab_calls=None, mb_left="9999"):
    def sab(request):
        mode = request.params["mode"]
        if mode == "queue":
            if sab_calls is not None:
                sab_calls.append(dict(request.params))
            ids = request.params.get("nzo_ids")
            shown = [s for s in slots if s["nzo_id"] in ids.split(",")] if ids else slots
            return (200, {}, json.dumps({"queue": {"status": "Downloading", "paused": False, "noofslots": len(shown),
                                                   "noofslots_total": len(slots) + 5, "kbpersec": "51200",
                                                   "mbleft": mb_left, "timeleft": "1:00:00", "slots": shown}}))
        return (200, {}, json.dumps({"history": {"slots": [
            {"nzo_id": FILM_ID, "name": "Quiet.Two.2020.1080p.BluRay", "category": "movies", "status": "Extracting",
             "bytes": 8 * 10**9, "action_line": "Unpacking 3/10", "completed": 0},
            {"nzo_id": "SABnzbd_nzo_done", "name": "Some.Thing", "category": "tv", "status": "Completed",
             "bytes": 10**9, "completed": NOW - 600}]}}))
    rsps.add_callback(responses.GET, f"{SAB}/api", callback=sab)

    def sonarr_q(request):
        if sonarr_calls is not None:
            sonarr_calls.append(1)
        recs = [{"downloadId": TV_ID, "seriesId": 2, "series": {"title": "Way Behind", "year": 2010, "tvdbId": 222},
                 "episode": {"seasonNumber": 2, "episodeNumber": e, "title": f"Ep {e}"},
                 "quality": {"quality": {"name": "WEBDL-1080p"}}, "trackedDownloadState": "downloading"} for e in (1, 2)]
        return (200, {}, json.dumps({"page": 1, "totalRecords": 2, "records": recs}))
    rsps.add_callback(responses.GET, f"{SON}/api/v3/queue", callback=sonarr_q)
    rsps.add(responses.GET, f"{RAD}/api/v3/queue", json={"page": 1, "totalRecords": 1, "records": [
        {"downloadId": FILM_ID, "movieId": 2, "movie": {"title": "Quiet Two", "year": 2020, "tmdbId": 502},
         "quality": {"quality": {"name": "Bluray-1080p"}}}]})


def test_watcher_matches_by_download_id_and_hides_other_categories(synced):
    c = synced
    calls = []
    w = dl.DownloadWatcher(None, None)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_live(rsps, [slot(TV_ID, "Way.Behind.S02.1080p.WEB.h264-GRP", "tv"),
                         slot(HIDDEN_ID, "Private.Thing.2160p", "hmm")], sonarr_calls=calls)
        assert w.tick(c, now=NOW) is True
        assert w.tick(c, now=NOW + 3) is True
        assert len(calls) == 1                                   # nothing new: Sonarr isn't asked again
    q = arr.download_queue(c, now=NOW + 3)
    [item] = q["items"]
    assert item["title"] == "Way Behind" and item["sub"] == "Season 2 · 2 episodes" and item["quality"] == "WEBDL-1080p"
    assert item["app"] == "sonarr" and item["pct"] == 40 and item["mb_left"] == 600.0
    assert q["hidden"] == 1 and q["categories"] == ["movies", "tv"]   # the 'hmm' job, grouped
    assert [r["kind"] for r in q["rows"]] == ["job", "other"]
    [pp] = q["pp"]
    assert pp["title"] == "Quiet Two" and pp["sub"] == "2020" and pp["status"] == "Extracting"
    assert [r["status"] for r in q["recent"]] == ["Completed"]
    assert "Private" not in json.dumps(q)


def test_watcher_looks_up_new_jobs_and_shows_all_when_asked(synced):
    c = synced
    calls = []
    w = dl.DownloadWatcher(None, None)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_live(rsps, [slot(TV_ID, "a", "tv")], sonarr_calls=calls)
        w.tick(c, now=NOW)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_live(rsps, [slot(TV_ID, "a", "tv"), slot(NEW_ID, "Brand.New.Film.2026.1080p", "other")], sonarr_calls=calls)
        w.tick(c, now=NOW + 3)                                   # too soon after the last full read
        assert len(calls) == 1
        w.tick(c, now=NOW + 15)
    assert len(calls) == 2                                       # a new job: the whole queue read, and looked up
    assert [r["kind"] for r in arr.download_queue(c, now=NOW + 15)["rows"]] == ["job", "other"]

    def retick(t):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            mock_live(rsps, [slot(TV_ID, "a", "tv"), slot(NEW_ID, "Brand.New.Film.2026.1080p", "other")])
            w.tick(c, now=t)
    settings.set(c, "sabnzbd.categories", "*")
    retick(NOW + 18)                                             # a new setting: the whole queue is read again
    q = arr.download_queue(c, now=NOW + 18)
    assert [i["title"] for i in q["items"]] == ["Way Behind", "Brand New Film 2026 1080p"]   # unmatched: tidied name
    settings.set(c, "sabnzbd.categories", "other")
    retick(NOW + 21)
    assert [i["title"] for i in arr.download_queue(c, now=NOW + 21)["items"]] == ["Way Behind", "Brand New Film 2026 1080p"]


def test_not_configured_and_idle(conn):
    assert arr.download_queue(conn) is None
    assert dl.DownloadWatcher(None, None).tick(conn) is False
    connect(conn)
    assert arr.download_queue(conn) == {"ready": False}


def test_downloads_page(logged_in, app_conn, app):
    assert "Downloads" not in logged_in.get("/").data.decode().split("<main")[0]      # no nav entry yet
    assert "Connect SABnzbd" in logged_in.get("/downloads").data.decode()
    connect(app_conn)
    plex_items(app_conn)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_all(rsps)
        arr_sync.run(app_conn, now=NOW)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_live(rsps, [slot(TV_ID, "Way.Behind.S02.1080p.WEB.h264-GRP", "tv"), slot(HIDDEN_ID, "Private.Thing", "hmm")])
        dl.DownloadWatcher(None, None).tick(app_conn)
    body = logged_in.get("/downloads").data.decode()
    assert "Way Behind" in body and "Season 2 · 2 episodes" in body and "WEBDL-1080p" in body
    assert "1 other download" in body and "Other items" in body and "Private" not in body
    assert "/img/arr/sonarr/2" in body and 'hx-trigger="every 2s' in body
    assert "Finishing up" in logged_in.get("/downloads/live").data.decode()
    assert 'href="/downloads"' in logged_in.get("/").data.decode()                     # in the nav now
    stats = logged_in.get("/insights/downloads").data.decode()
    assert "Private" not in stats and "See the queue" in stats


def test_arr_poster_proxy(logged_in, app_conn):
    connect(app_conn)
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{SON}/api/v3/mediacover/2/poster-250.jpg", body=b"\xff\xd8jpeg",
                 content_type="image/jpeg")
        r = logged_in.get("/img/arr/sonarr/2")
        assert r.status_code == 200 and r.data == b"\xff\xd8jpeg"
        assert rsps.calls[0].request.headers["X-Api-Key"] == "sonarr-key"
    assert logged_in.get("/img/arr/sonarr/2").status_code == 200            # from the disk cache now
    assert logged_in.get("/img/arr/plex/2").status_code == 404


def test_settings_categories(conn):
    arr.save(conn, {"sabnzbd.categories": " tv ,movies,, music "})
    assert settings.get(conn, "sabnzbd.categories") == "tv, movies, music"
    assert arr.visible_categories(conn) == {"tv", "movies", "music"}
    arr.save(conn, {"sabnzbd.categories": "*"})
    assert arr.visible_categories(conn) is None



def test_runs_of_other_jobs_are_grouped():
    """Marc's example: 1-4 other, 5 TV, 6-9 other, 10 a film."""
    def s(i, cat, mb, eta):
        return {"nzo_id": f"n{i}", "name": f"x{i}", "cat": cat, "status": "Downloading" if i == 1 else "Queued",
                "pct": 50 if i == 1 else 0, "mb": float(mb), "mb_left": float(mb) / (2 if i == 1 else 1),
                "time_left": None, "eta_s": eta, "priority": "Normal", "index": i - 1}
    slots = [s(1, "hmm", 2000, 300), s(2, "hmm", 2000, 600), s(3, "hmm", 2000, 900), s(4, "hmm", 2000, 1320),
             s(5, "tv", 2000, 1440), s(6, "hmm", 4000, 1800), s(7, "hmm", 4000, 2200), s(8, "hmm", 4000, 2600),
             s(9, "hmm", 4000, 2940), s(10, "movies", 5000, 3720)]
    rows = dl.group_rows(slots, lambda j: j["cat"] in ("tv", "movies"))
    assert [(r["kind"], r.get("count"), r.get("eta_s")) for r in rows] == [
        ("other", 4, 1320), ("job", None, 1440), ("other", 4, 2940), ("job", None, 3720)]
    assert rows[0]["mb"] == 8000 and rows[0]["mb_left"] == 7000 and rows[0]["downloading"]
    assert rows[2]["mb"] == 16000 and not rows[2]["downloading"]


def test_sab_seconds_and_eta_filter():
    from bingestats.web.filters import eta
    assert parse.sab_seconds("0:04:12") == 252 and parse.sab_seconds("6:22:40") == 22960
    assert parse.sab_seconds("1:06:22:40") == 109360 and parse.sab_seconds("x") is None
    assert [eta(x) for x in (45, 60, 1320, 3720, 22960, 109360)] == ["45 secs", "1 min", "22 mins", "1h 02m", "6h 22m", "1d 6h"]


def test_between_full_reads_only_the_jobs_on_show_are_asked_for(synced, caplog):
    c = synced
    w = dl.DownloadWatcher(None, None)
    others = [slot(f"o{i}", f"Other.{i}", "hmm", mb=1024.0, left=1024.0) for i in range(3)]
    tv = slot(TV_ID, "Way.Behind.S02.1080p.WEB.h264-GRP", "tv", mb=2048.0, left=2048.0)
    calls = []
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_live(rsps, others + [tv], sab_calls=calls, mb_left=str(3 * 1024 + 2048))
        w.tick(c, now=NOW)
    [g, job] = w.st["base"]
    assert g["count"] == 3 and "nzo_ids" not in calls[0]
    # 2 GB of the other jobs done since; the TV job's half done. 50 MB/s.
    done_others = [slot(f"o{i}", f"Other.{i}", "hmm", mb=1024.0, left=1024.0 if i == 2 else 0.0) for i in range(3)]
    tv_now = slot(TV_ID, "Way.Behind.S02.1080p.WEB.h264-GRP", "tv", mb=2048.0, left=1024.0)
    calls.clear()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_live(rsps, done_others + [tv_now], sab_calls=calls, mb_left=str(1024 + 1024))
        w.tick(c, now=NOW + 3)
    assert [x.get("nzo_ids") for x in calls] == [TV_ID] and calls[0].get("timeout") is None
    rows = arr.download_queue(c, now=NOW + 3)["rows"]
    assert rows[0]["kind"] == "other" and rows[0]["count"] == 3 and rows[0]["mb_left"] == 1024.0
    assert rows[0]["eta_s"] == 20 and rows[1]["eta_s"] == 40           # MB ahead / (51200 KB/s = 50 MB/s)
    assert rows[1]["mb_left"] == 1024.0

    # SABnzbd stops answering: the last snapshot stays, with one warning
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.GET, f"{SAB}/api", body=requests.ConnectionError("down"))
        with caplog.at_level("WARNING", logger="bingestats.downloads"):
            assert w.tick(c, now=NOW + 6) is True
            w.tick(c, now=NOW + 9)
    assert len([r for r in caplog.records if "SABnzbd queue" in r.getMessage()]) == 1
    assert arr.download_queue(c, now=NOW + 9)["rows"][1]["mb_left"] == 1024.0


# -- moving jobs in SABnzbd's queue -----------------------------------------------------------

PRIO = {"Low": -1, "Normal": 0, "High": 1, "Force": 2}


class FakeQueue:
    """SABnzbd's queue as it behaved on 5.1.3: switch takes the job out, then inserts it at the
    target's old index (so before the target moving up, after it moving down), and the job
    takes the target's priority. nzo_ids and start/limit filter the slots, renumbering index."""

    def __init__(self, jobs):            # [(nzo_id, cat, priority)]
        self.jobs = [{"nzo_id": n, "cat": c, "priority": p} for n, c, p in jobs]
        self.calls: list[dict] = []

    def order(self):
        return [j["nzo_id"] for j in self.jobs]

    def __call__(self, request):
        p = dict(request.params)
        self.calls.append(p)
        if p["mode"] == "switch":
            ids = self.order()
            if p["value"] not in ids or p["value2"] not in ids:
                return (200, {}, json.dumps({"result": {"position": -1, "priority": -1}}))
            job, target = self.jobs[ids.index(p["value"])], self.jobs[ids.index(p["value2"])]
            at = ids.index(p["value2"])
            self.jobs.remove(job)
            job["priority"] = target["priority"]
            self.jobs.insert(at, job)
            return (200, {}, json.dumps({"result": {"position": at, "priority": PRIO[job["priority"]]}}))
        if p["mode"] == "history":
            return (200, {}, json.dumps({"history": {"slots": []}}))
        js = self.jobs
        if p.get("nzo_ids"):
            js = [j for j in js if j["nzo_id"] in p["nzo_ids"].split(",")]
        start, limit = int(p.get("start", 0)), int(p.get("limit", 0))
        js = js[start:start + limit] if limit else js[start:]
        slots = [slot(j["nzo_id"], j["nzo_id"], j["cat"], left=100.0, tl="0:00:10") | {"priority": j["priority"], "index": i}
                 for i, j in enumerate(js)]
        return (200, {}, json.dumps({"queue": {"status": "Downloading", "paused": False, "noofslots": len(slots),
                                               "noofslots_total": len(self.jobs), "kbpersec": "1024",
                                               "mbleft": str(100.0 * len(self.jobs)), "timeleft": "0:10:00",
                                               "slots": slots}}))


def N(x):
    return f"SABnzbd_nzo_{x}"


@pytest.fixture
def queue(synced):
    """Two runs of other jobs and three TV/film jobs, read once by the watcher; moves allowed."""
    c = synced
    settings.set(c, "sabnzbd.allow_moves", True)
    fq = FakeQueue([(N("o1"), "hmm", "High"), (N("o2"), "hmm", "Normal"), (N("tv1"), "tv", "Normal"),
                    (N("o3"), "hmm", "Normal"), (N("o4"), "hmm", "Normal"), (N("film"), "movies", "Normal"),
                    (N("tv2"), "tv", "Normal"), (N("o5"), "hmm", "Normal")])
    w = dl.DownloadWatcher(None, None)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, f"{SAB}/api", callback=fq)
        rsps.add(responses.GET, f"{SON}/api/v3/queue", json={"page": 1, "totalRecords": 0, "records": []})
        rsps.add(responses.GET, f"{RAD}/api/v3/queue", json={"page": 1, "totalRecords": 0, "records": []})
        w.tick(c, now=NOW)
    return c, fq, w


def run(fq, fn):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, f"{SAB}/api", callback=fq)
        return fn()


def test_sab_client_only_moves_when_allowed_and_never_writes_otherwise():
    sab = SabClient(SAB, "k")
    for mode in ("switch", "delete", "pause", "change_cat", "priority"):
        with pytest.raises(ArrError):
            sab.get(mode)
    with pytest.raises(ArrError, match="turned off"):
        sab.move(N("a"), N("b"), allowed=False)
    for bad in ("a&mode=delete", "x", "a b c d e f", ""):
        with pytest.raises(ArrError, match="job id"):
            sab.move(bad, N("b"), allowed=True)
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{SAB}/api", json={"result": {"position": 3, "priority": 1}})
        assert sab.move(N("a"), N("b"), allowed=True) == {"position": 3, "priority": 1}
        q = rsps.calls[0].request.params
        assert (q["mode"], q["value"], q["value2"]) == ("switch", N("a"), N("b"))
    with responses.RequestsMock() as rsps:                      # SABnzbd 5's ids are UUIDs
        rsps.add(responses.GET, f"{SAB}/api", json={"result": {"position": 0, "priority": 0}})
        sab.move("61fb3d9a-5559-4480-9e2c-dc9b97d3369e", "5a089378-fa98-4b8b-ae75-369ca7863c3a", allowed=True)
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, f"{SAB}/api", json={"result": {"position": -1, "priority": -1}})
        with pytest.raises(ArrError, match="didn't move"):
            sab.move(N("a"), N("b"), allowed=True)


def test_groups_carry_their_edges(queue):
    _c, _fq, w = queue
    assert [(r["kind"], r.get("first_id"), r.get("last_id")) for r in w.st["base"]] == [
        ("other", N("o1"), N("o2")), ("job", None, None), ("other", N("o3"), N("o4")), ("job", None, None),
        ("job", None, None), ("other", N("o5"), N("o5"))]


@pytest.mark.parametrize("me, above, below, expect, switches", [
    # up, past a group: one move onto the job below the drop
    ("film", "o2", "tv1", ["o1", "o2", "film", "tv1", "o3", "o4", "tv2", "o5"], 1),
    # down, past a group: one move onto the job above the drop
    ("tv1", "o4", "film", ["o1", "o2", "o3", "o4", "tv1", "film", "tv2", "o5"], 1),
    # to the very top, and the very bottom
    ("tv2", None, "o1", ["tv2", "o1", "o2", "tv1", "o3", "o4", "film", "o5"], 1),
    ("tv1", "o5", None, ["o1", "o2", "o3", "o4", "film", "tv2", "o5", "tv1"], 1),
    # already there: nothing sent
    ("film", "o4", "tv2", ["o1", "o2", "tv1", "o3", "o4", "film", "tv2", "o5"], 0),
])
def test_a_drop_lands_exactly_between_its_neighbours(queue, me, above, below, expect, switches):
    c, fq, _w = queue
    before = len([x for x in fq.calls if x["mode"] == "switch"])
    res = run(fq, lambda: arr.move_job(c, N(me), above=above and N(above), below=below and N(below)))
    assert fq.order() == [N(x) for x in expect]
    assert len([x for x in fq.calls if x["mode"] == "switch"]) - before == switches and res["moved"] == switches


def test_a_vanished_neighbour_still_lands_right(queue):
    c, fq, _w = queue
    fq.jobs = [j for j in fq.jobs if j["nzo_id"] != N("o4")]          # o4 finished since the page was drawn
    run(fq, lambda: arr.move_job(c, N("tv1"), above=N("o4"), below=N("film")))
    assert fq.order().index(N("tv1")) == fq.order().index(N("film")) - 1
    fq.jobs = [j for j in fq.jobs if j["nzo_id"] != N("o1")]          # moving up with the job above gone
    run(fq, lambda: arr.move_job(c, N("tv2"), above=N("o1"), below=N("o2")))
    assert fq.order()[:2] == [N("tv2"), N("o2")]
    with pytest.raises(ValueError, match="changed"):
        arr.plan_move(N("x"), N("gone"), None, [N("x")])


def test_dropping_at_the_top_works_when_the_downloading_job_has_finished(queue):
    """The top group starts with the job that's downloading; it can finish before the drop."""
    c, fq, _w = queue
    fq.jobs = fq.jobs[1:]                                              # o1 done since the page was read
    res = run(fq, lambda: arr.move_job(c, N("film"), above=None, below=N("o1")))
    assert fq.order()[0] == N("film") and res["moved"] == 1
    fq.jobs = [j for j in fq.jobs if j["nzo_id"] not in (N("o3"), N("o4"))]   # both neighbours gone
    run(fq, lambda: arr.move_job(c, N("tv2"), above=N("o3"), below=N("o4")))
    assert fq.order()[0] == N("tv2")
    run(fq, lambda: arr.move_job(c, N("tv1"), above=N("gone"), below=None))  # the bottom is the bottom
    assert fq.order()[-1] == N("tv1")


def test_moving_onto_a_high_job_says_so_and_queues_one_refresh(queue):
    c, fq, _w = queue
    res = run(fq, lambda: arr.move(c, N("film"), to="top"))
    assert fq.order()[0] == N("film") and res["priority_name"] == "High" and res["at"]
    run(fq, lambda: arr.move(c, N("tv2"), to="bottom"))
    assert fq.order()[-1] == N("tv2")
    assert c.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'downloads_refresh' AND status = 'queued'").fetchone()[0] == 1


def test_shown_first_keeps_their_order(queue):
    c, fq, _w = queue
    res = run(fq, lambda: arr.shown_first(c))
    assert fq.order()[:3] == [N("tv1"), N("film"), N("tv2")] and res["moved"] == 3
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(arr, "MAX_FRONT", 2)
        with pytest.raises(ValueError, match="limit"):
            arr.shown_first(c)


def test_moves_refused_when_off_or_not_on_show(queue):
    c, fq, _w = queue
    with pytest.raises(ValueError, match="isn't on the Downloads page"):
        run(fq, lambda: arr.move_job(c, N("o3"), below=N("o1")))       # hidden jobs can't be moved from here
    with pytest.raises(ValueError, match="where"):
        run(fq, lambda: arr.move_job(c, N("tv1")))
    with pytest.raises(ValueError, match="top or bottom"):
        arr.move(c, N("tv1"), to="sideways")
    settings.set(c, "sabnzbd.allow_moves", False)
    with pytest.raises(PermissionError):
        arr.move(c, N("tv1"), to="top")
    assert not [x for x in fq.calls if x["mode"] == "switch"]


def test_refresh_job_makes_the_next_read_a_full_one(queue):
    c, fq, w = queue
    from bingestats.worker.jobs_runner import run_job
    full = lambda: [x for x in fq.calls if x["mode"] == "queue" and "nzo_ids" not in x and x.get("limit") == "0"]  # noqa: E731
    n = len(full())
    run(fq, lambda: w.tick(c, now=NOW + 3))
    assert len(full()) == n                                            # a fast read
    run_job(c, None, {"id": 0, "kind": "downloads_refresh", "params": {}})
    run(fq, lambda: w.tick(c, now=NOW + 6))
    assert len(full()) == n + 1 and not dl.REFRESH.is_set()


def test_move_routes(logged_in, app_conn, app):
    c = app_conn
    connect(c)
    fq = FakeQueue([(N("o1"), "hmm", "Normal"), (N("tv1"), "tv", "Normal"), (N("tv2"), "tv", "Normal")])
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(responses.GET, f"{SAB}/api", callback=fq)
        rsps.add(responses.GET, f"{SON}/api/v3/queue", json={"page": 1, "totalRecords": 0, "records": []})
        rsps.add(responses.GET, f"{RAD}/api/v3/queue", json={"page": 1, "totalRecords": 0, "records": []})
        dl.DownloadWatcher(None, None).tick(c)
    body = logged_in.get("/downloads").data.decode()
    assert "drag-handle" not in body and "sortable.min.js" not in body       # off by default
    r = logged_in.post("/downloads/move", {"nzo_id": N("tv1"), "to": "top"})
    assert r.status_code == 403 and "turned off" in r.json["error"]

    logged_in.post("/settings/arr", {"sabnzbd.url": SAB, "sabnzbd.categories": "", "sabnzbd.allow_moves": "on",
                                     "sonarr.url": SON, "radarr.url": RAD})
    assert settings.get(c, "sabnzbd.allow_moves") is True
    body = logged_in.get("/downloads").data.decode()
    assert "drag-handle" in body and "sortable.min.js" in body and "Move to top" in body
    assert f'data-id="{N("tv1")}"' in body and f'data-first="{N("o1")}"' in body and "TV &amp; films first" in body
    assert 'checked' in logged_in.get("/settings").data.decode().split('name="sabnzbd.allow_moves"')[1][:20]

    r = logged_in.client.post("/downloads/move", data={"nzo_id": N("tv1"), "to": "top"})   # no CSRF token
    assert r.status_code == 400
    r = run(fq, lambda: logged_in.post("/downloads/move", {"nzo_id": N("tv1"), "above": "", "below": N("o1")}))
    assert r.status_code == 200 and r.json["ok"] and fq.order() == [N("tv1"), N("o1"), N("tv2")]

    ro = {"Authorization": f"Bearer {api_tokens.create(c, 'ro', 'read')}"}
    rw = {"Authorization": f"Bearer {api_tokens.create(c, 'rw', 'write')}"}
    api = app.test_client()
    assert api.post(f"/api/v1/downloads/{N('tv1')}/move", json={"to": "bottom"}, headers=ro).status_code == 403
    r = run(fq, lambda: api.post(f"/api/v1/downloads/{N('tv1')}/move", json={"to": "bottom"}, headers=rw))
    assert r.status_code == 200 and fq.order() == [N("o1"), N("tv2"), N("tv1")]
    r = api.post(f"/api/v1/downloads/{N('o1')}/move", json={"to": "top"}, headers=rw)
    assert r.status_code == 400
