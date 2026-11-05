"""Pins and the pinned calendar (docs/specs/pins.md): episode state, sync, the service and the pages."""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import responses

from bingestats import db
from bingestats.services import accounts, jobs, pins, settings
from bingestats.worker import arr_sync, jobs_runner
from tests.test_arr import SON, connect, ep, mock_all, series_json

LON = ZoneInfo("Europe/London")
NOW = int(datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc).timestamp())   # a Thursday, noon in London
H, D = 3600, 86400
OWNER, ALEX = 1, 11


# -- episode state ---------------------------------------------------------------------------

def state(air, **kw):
    return pins.episode_state(air, now=NOW, tz=LON, **{"in_plex": False, **kw})


def test_in_plex_outranks_everything():
    assert state(NOW + 5 * D, in_plex=True, monitored=False, downloading=True) == pins.AVAILABLE


def test_on_disk_but_not_in_plex_is_said_so_not_missing():
    assert state(NOW - 9 * D, has_file=True) == pins.ON_DISK


def test_unmonitored_is_never_missing():
    assert state(NOW - 9 * D, monitored=False) == pins.UNMONITORED


def test_downloading_beats_missing():
    assert state(NOW - 9 * D, downloading=True) == pins.DOWNLOADING


def test_today_is_local_and_covers_already_aired():
    assert state(NOW - 3 * H) == pins.AIRING_TODAY                 # aired at 09:00, not here yet
    assert state(NOW + 9 * H) == pins.AIRING_TODAY                 # 21:00 tonight
    assert state(NOW + 12 * H + 30 * 60) == pins.UPCOMING          # 00:30 tomorrow, London time


def test_upcoming_awaiting_missing():
    assert state(None) == pins.UPCOMING
    assert state(NOW + 2 * D) == pins.UPCOMING
    assert state(NOW - 30 * H) == pins.AWAITING                    # yesterday, within 48 h
    assert state(NOW - 49 * H) == pins.MISSING


# -- fixtures ---------------------------------------------------------------------------------

def library(c):
    c.execute("INSERT OR IGNORE INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    c.execute("INSERT INTO users (id, username, friendly_name, is_owner, share_salt) VALUES (?, 'marc', 'Marc', 1, 's')",
              (OWNER,))
    c.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (?, 'alexp', 'Alex', 's')", (ALEX,))
    c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, tvdb_id, thumb, year) VALUES "
              "(10, 1, 'show', 'plex://show/p', 'Pinned Show', 111, '/library/metadata/10/thumb/1', 2020)")
    c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, grandparent_rating_key, "
              "season_index, episode_index) VALUES (101, 1, 'episode', 'plex://episode/1', 'Pilot', 10, 1, 1)")
    c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, tvdb_id) VALUES "
              "(30, 1, 'show', 'plex://show/u', 'Unpinned', 333)")
    for sid, title, tvdb, nxt in ((1, "Pinned Show", 111, NOW + 2 * H), (2, "Sonarr Only", 222, NOW + 40 * D),
                                  (3, "Unpinned", 333, NOW + D)):
        c.execute("INSERT INTO sonarr_series (id, title, tvdb_id, status, next_airing) VALUES (?, ?, ?, 'continuing', ?)",
                  (sid, title, tvdb, nxt))
    rows = [(1, 1, 1, "Pilot", NOW - 10 * D, 1, 1), (1, 1, 2, "Late", NOW - 3 * D, 0, 1),
            (1, 1, 3, "Soon", NOW - 30 * H, 0, 1), (1, 1, 4, "Tonight", NOW + 2 * H, 0, 1),
            (1, 1, 5, "Later", NOW + 3 * D, 0, 1), (1, 1, 6, "Fetching", NOW - 2 * D, 0, 1),
            (1, 0, 1, "Christmas", NOW + D, 0, 1), (1, 1, 7, "Skipped", NOW + 4 * D, 0, 0),
            (1, 1, 8, "Grabbed", NOW - 4 * D, 1, 1), (1, 1, 9, "Ancient", NOW - 60 * D, 0, 1),
            (3, 1, 1, "Not mine", NOW + D, 0, 1)]
    c.executemany("INSERT INTO sonarr_episodes (series_id, season, episode, title, air_at, has_file, monitored) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    live = {"rows": [{"kind": "job", "nzo_id": "N1", "pct": 40}], "pp": []}
    known = {"N1": [{"app": "sonarr", "tvdb_id": 111, "season": 1, "episode": 6}]}
    c.execute("INSERT INTO arr_status (app, ok, info) VALUES ('sabnzbd', 1, ?)", (json.dumps({"live": live, "live_map": known}),))
    return c


@pytest.fixture
def lib(conn):
    return library(conn)


# -- the service ------------------------------------------------------------------------------

def test_pin_unpin_toggle(lib):
    assert pins.owner_id(lib) == OWNER
    assert pins.pin(lib, OWNER, 111) is True and pins.pin(lib, OWNER, 111) is False
    assert pins.pin(lib, OWNER, 222)                               # in Sonarr, not in Plex: fine
    with pytest.raises(ValueError):
        pins.pin(lib, OWNER, 999)                                  # nowhere
    assert pins.pinned(lib, OWNER) == {111, 222} and pins.pinned(lib, ALEX) == set()
    assert pins.toggle(lib, OWNER, 111) is False and pins.pinned(lib, OWNER) == {222}


def test_a_new_pin_queues_its_episode_list_only_with_sonarr(lib):
    pins.toggle(lib, ALEX, 111)
    assert lib.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    connect(lib)
    pins.toggle(lib, ALEX, 222)
    assert [r[0] for r in lib.execute("SELECT kind FROM jobs")] == ["pin_guides"]


def test_find(lib):
    pins.pin(lib, OWNER, 111)
    hits = pins.find(lib, "pinned sh", OWNER)
    assert [(h["title"], h["pinned"], h["rating_key"]) for h in hits] == [("Pinned Show", True, 10)]
    assert [h["title"] for h in pins.find(lib, "only", OWNER)] == ["Sonarr Only"]
    assert pins.find(lib, "o", OWNER) == []                        # too short to search


def test_downloading_from_the_live_queue(lib):
    assert pins.downloading(lib) == {(111, 1, 6): 40}


def test_calendar(lib):
    pins.pin(lib, OWNER, 111)
    pins.pin(lib, OWNER, 222)
    cal = pins.calendar(lib, OWNER, now=NOW)
    agenda = {d["label"]: [(e["episode"], e["state"]) for e in d["episodes"]] for d in cal["agenda"]}
    assert agenda == {"Today": [(4, "airing_today")], "Sunday 8 November": [(5, "upcoming")]}
    # aired, not watchable: newest first; the special and the unmonitored one are hidden,
    # the one in Plex isn't here, and 60 days ago is too old to be news
    assert [(e["episode"], e["state"]) for e in cal["overdue"]] == [
        (3, "awaiting"), (6, "downloading"), (2, "missing"), (8, "on_disk")]
    assert cal["overdue"][1]["pct"] == 40
    [later] = cal["later"]
    assert later["title"] == "Sonarr Only" and later["dated"] and later["rating_key"] is None
    assert cal["undated"] == []                                    # every pin is listed above
    assert [s["title"] for s in cal["pins"]] == ["Pinned Show", "Sonarr Only"]


def test_shows_with_nothing_to_come_are_listed_once(lib):
    lib.execute("INSERT INTO sonarr_series (id, title, tvdb_id, status) VALUES (4, 'Finished', 444, 'ended')")
    lib.execute("INSERT INTO sonarr_series (id, title, tvdb_id, status) VALUES (5, 'Stalled', 555, 'continuing')")
    lib.execute("INSERT INTO sonarr_episodes VALUES (5, 2, 1, 'Lost', ?, 0, 1)", (NOW - 5 * D,))
    for t in (111, 444, 555):
        pins.pin(lib, OWNER, t)
    cal = pins.calendar(lib, OWNER, now=NOW)
    assert [s["title"] for s in cal["undated"]] == ["Finished"]     # Stalled is under aired-not-arrived
    assert "Stalled" in [e["show"] for e in cal["overdue"]] and cal["later"] == []
    assert all(e["show"] != "Unpinned" for d in cal["agenda"] for e in d["episodes"])


def test_calendar_settings_show_specials_and_unmonitored(lib):
    pins.pin(lib, OWNER, 111)
    settings.set(lib, "pins.show_specials", True)
    settings.set(lib, "pins.show_unmonitored", True)
    eps = [(e["season"], e["episode"], e["state"]) for d in pins.calendar(lib, OWNER, now=NOW)["agenda"]
           for e in d["episodes"]]
    assert (0, 1, "upcoming") in eps and (1, 7, "unmonitored") in eps


def test_calendar_is_per_person(lib):
    pins.pin(lib, ALEX, 222)
    cal = pins.calendar(lib, ALEX, now=NOW)
    assert cal["agenda"] == [] and cal["overdue"] == [] and [s["title"] for s in cal["pins"]] == ["Sonarr Only"]
    assert pins.calendar(lib, OWNER, now=NOW)["count"] == 0


def test_for_item(lib):
    show = {"type": "show", "rating_key": 10, "tvdb_id": 111}
    episode = {"type": "episode", "rating_key": 101, "grandparent_rating_key": 10}
    got = pins.for_item(lib, show, OWNER)
    assert (got["tvdb_id"], got["pinned"]) == (111, False) and "outlook" in got
    pins.pin(lib, OWNER, 111)
    got = pins.for_item(lib, episode, OWNER)
    assert (got["tvdb_id"], got["pinned"]) == (111, True)
    assert pins.for_item(lib, {"type": "movie", "rating_key": 20}, OWNER) is None
    assert pins.for_item(lib, show, None) is None


# -- sync ------------------------------------------------------------------------------------

def guide_mocks(rsps, calls, *, fail_for=None):
    def guide(request):
        sid = int(request.params["seriesId"])
        calls.append(sid)
        if sid == fail_for:
            return (500, {}, "")
        return (200, {}, json.dumps([ep(sid, 1, e, air="2025-01-01T20:00:00Z", has_file=True) for e in (1, 2)]))
    rsps.add_callback(responses.GET, f"{SON}/api/v3/episode", callback=guide)


def test_sync_reads_the_wide_calendar_and_guides_for_pins_only(conn):
    connect(conn)
    library(conn)
    conn.execute("DELETE FROM sonarr_episodes")
    calls = []
    with responses.RequestsMock() as rsps:
        mock_all(rsps)
        guide_mocks(rsps, calls)
        arr_sync.run(conn, now=NOW)                                # no pins: no guides read
        assert calls == []
        pins.pin(conn, ALEX, 444)                                  # "Keeping Up", Sonarr series 4
        result = arr_sync.run(conn, now=NOW)
        cal_calls = [c.request.params for c in rsps.calls if c.request.url.startswith(f"{SON}/api/v3/calendar")]
    assert calls == [4]
    assert {"start": "2026-10-29", "end": "2027-01-04", "unmonitored": "true"}.items() <= cal_calls[-1].items()
    rows = conn.execute("SELECT series_id, season, episode FROM sonarr_episodes ORDER BY 1, 2, 3").fetchall()
    assert [tuple(r) for r in rows] == [(2, 3, 1), (4, 1, 1), (4, 1, 2), (4, 1, 4)]
    assert result["synced"]["sonarr"]["episodes"] == 4


def test_a_guide_that_fails_doesnt_fail_the_sync(conn):
    connect(conn)
    library(conn)
    calls = []
    with responses.RequestsMock() as rsps:
        mock_all(rsps)
        arr_sync.run(conn, now=NOW)
        pins.pin(conn, ALEX, 444)
        pins.pin(conn, ALEX, 222)                                  # "Way Behind", series 2
        guide_mocks(rsps, calls, fail_for=2)
        result = arr_sync.run(conn, now=NOW)
    assert result["errors"] == {} and sorted(calls) == [2, 4]
    assert conn.execute("SELECT COUNT(*) FROM sonarr_episodes WHERE series_id = 4").fetchone()[0] == 3


def test_series_keeps_network_genres_and_tmdb_id(conn):
    connect(conn)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        s = series_json(9, "Telly", 999, ended=False, files=1, episodes=1, seasons=[(1, 1, 1)])
        s.update(network="BBC One", genres=["Drama", "Crime"], tmdbId=4242)
        mock_all(rsps)
        rsps.replace(responses.GET, f"{SON}/api/v3/series", json=[s])
        arr_sync.run(conn, now=NOW)
    r = conn.execute("SELECT network, genres, tmdb_id FROM sonarr_series").fetchone()
    assert (r["network"], json.loads(r["genres"]), r["tmdb_id"]) == ("BBC One", ["Drama", "Crime"], 4242)


def test_pin_guides_job(conn, tmp_path):
    connect(conn)
    library(conn)
    with responses.RequestsMock() as rsps:
        mock_all(rsps)
        arr_sync.run(conn, now=NOW)
    pins.toggle(conn, ALEX, 444)
    job = jobs.claim_next(conn)
    assert job["kind"] == "pin_guides"

    class Cfg:
        data_dir = tmp_path
    calls = []
    with responses.RequestsMock() as rsps:
        guide_mocks(rsps, calls)
        assert jobs_runner.run_job(conn, Cfg, job) == {"series": 1, "episodes": 2}
    assert calls == [4]


def test_migration_10(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "v9.db")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:9])
    monkeypatch.setattr(db, "LATEST", 9)
    db.migrate(c)
    c.execute("INSERT INTO sonarr_series (id, title, tvdb_id) VALUES (1, 'Old', 5)")
    monkeypatch.undo()
    assert db.migrate(c) == db.LATEST
    r = c.execute("SELECT title, network, genres, tmdb_id FROM sonarr_series").fetchone()
    assert tuple(r) == ("Old", None, "[]", None)
    for t in ("pins", "sonarr_episodes", "tmdb_shows", "pin_notified"):
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0


# -- pages -----------------------------------------------------------------------------------

@pytest.fixture
def site(logged_in, app_conn):
    connect(app_conn)
    library(app_conn)
    return logged_in


def test_calendar_page_and_nav(site, app_conn):
    body = site.get("/calendar").data.decode()
    assert 'href="/calendar"' in body and "Pin some shows" in body
    pins.pin(app_conn, OWNER, 111)
    body = site.get("/calendar").data.decode()
    assert "Pinned Show" in body and "Coming up" in body
    assert 'action="/pins/111"' in body                            # unpin straight from the episode row
    assert 'href="/item/10"' in body and "&#34;" not in body       # the episode row links to the show, unescaped
    assert "No future dates planned" not in body                   # it's listed above already


def test_no_calendar_in_nav_without_sonarr(logged_in, app_conn):
    assert 'href="/calendar"' not in logged_in.get("/").data.decode()


def test_calendar_without_an_owner_says_why(logged_in, app_conn):
    connect(app_conn)
    body = logged_in.get("/calendar").data.decode()
    assert "server owner isn't in Users" in body
    assert logged_in.post("/pins/111").status_code == 409


def test_a_pin_reloads_the_calendar_without_a_page_refresh(site, app_conn):
    page = site.get("/calendar").data.decode()
    assert 'hx-get="/calendar/body"' in page and "pins-changed from:body" in page
    r = site.post("/pins/222", headers={"HX-Request": "true"})
    assert r.headers["HX-Trigger"] == "pins-changed"              # tells the page to reload its body
    body = site.get("/calendar/body", headers={"HX-Request": "true"}).data.decode()
    assert "Sonarr Only" in body and "Pin some shows" not in body and "<html" not in body
    site.post("/pins/222", headers={"HX-Request": "true"})
    assert "Pin some shows" in site.get("/calendar/body").data.decode()


def test_pin_from_the_page(site, app_conn):
    r = site.post("/pins/111", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Pinned" in r.data.decode() and pins.pinned(app_conn, OWNER) == {111}
    r = site.post("/pins/111", {"next": "/item/10"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/item/10") and pins.pinned(app_conn, OWNER) == set()
    assert site.post("/pins/999").status_code == 404
    assert site.post("/pins/111", {"next": "//evil.example"}).headers["Location"].endswith("/calendar")


def test_find_partial(site, app_conn):
    body = site.get("/calendar/find?q=sonarr", headers={"HX-Request": "true"}).data.decode()
    assert "Sonarr Only" in body and "Not in Plex yet" in body and "/pins/222" in body


def test_item_page_pin_button(site, app_conn):
    assert 'action="/pins/111"' in site.get("/item/10").data.decode()
    assert 'action="/pins/111"' in site.get("/item/101").data.decode()     # an episode pins its show


def test_viewer_pins_are_their_own(app, site, app_conn):
    from tests.conftest import Browser
    accounts.set_password(app_conn, ALEX, "alex-password")
    b = Browser(app.test_client())
    b.get("/login")
    assert b.post("/login", {"username": "alexp", "password": "alex-password"}).status_code == 302
    nav = b.get("/me").data.decode()
    assert 'href="/me/calendar"' in nav
    pins.pin(app_conn, OWNER, 111)
    assert b.post("/me/pins/222", headers={"HX-Request": "true"}).status_code == 200
    assert pins.pinned(app_conn, ALEX) == {222} and pins.pinned(app_conn, OWNER) == {111}
    body = b.get("/me/calendar").data.decode()
    assert "Sonarr Only" in body and "Pinned Show" not in body and "/item/" not in body
    assert "/me/calendar/find" in body and 'hx-get="/me/calendar/body"' in body
    assert "Sonarr Only" in b.get("/me/calendar/body").data.decode()
    assert b.post("/pins/111").status_code == 403                  # the admin's pins are closed to them
    assert b.get("/calendar").status_code == 302
    assert "Pinned Show" in b.get("/me/calendar/find?q=pinned").data.decode()
    part = b.get("/me/calendar/month").data.decode()
    assert 'id="cal-month"' in part and "/me/calendar/month?m=" in part and "/item/" not in part


# -- month view ------------------------------------------------------------------------------

def test_month(lib):
    pins.pin(lib, OWNER, 111)
    m = pins.month(lib, OWNER, now=NOW)                                # November 2026 starts on a Sunday
    assert (m["title"], m["prev"], m["next"], m["is_this_month"]) == ("November 2026", "2026-10", "2026-12", True)
    assert len(m["weeks"]) == 6 and all(len(w) == 7 for w in m["weeks"])
    assert m["weeks"][0][0]["date"].isoformat() == "2026-10-26" and not m["weeks"][0][0]["in_month"]
    days = {d["date"].day: [(e["episode"], e["state"]) for e in d["episodes"]] for d in m["days"]}
    assert days == {1: [(8, "on_disk")], 2: [(2, "missing")], 3: [(6, "downloading")], 4: [(3, "awaiting")],
                    5: [(4, "airing_today")], 8: [(5, "upcoming")]}                # no special, no unmonitored
    assert m["count"] == 6
    [today] = [d for w in m["weeks"] for d in w if d["today"]]
    assert today["date"].day == 5
    oct26 = m["weeks"][0][0]
    assert [e["episode"] for e in oct26["episodes"]] == [1]           # the grid's edge days carry their episodes too


def test_month_other_months_and_bad_input(lib):
    pins.pin(lib, OWNER, 111)
    m = pins.month(lib, OWNER, "2026-09", now=NOW)
    assert m["title"] == "September 2026" and not m["is_this_month"]
    assert [d["date"].day for d in m["days"]] == [6]                   # "Ancient", 60 days back
    assert pins.month(lib, OWNER, "nonsense", now=NOW)["title"] == "November 2026"
    assert pins.month(lib, OWNER, "2026-13", now=NOW)["title"] == "November 2026"
    assert pins.month(lib, ALEX, now=NOW)["count"] == 0


def test_month_pages(site, app_conn):
    pins.pin(app_conn, OWNER, 111)
    page = site.get("/calendar").data.decode()
    assert 'id="cal-month"' in page and "/calendar/month?m=" in page and "Pinned Show" in page
    part = site.get("/calendar/month?m=2026-01", headers={"HX-Request": "true"}).data.decode()
    assert "January 2026" in part and "<html" not in part and "This month" in part
