"""Stage 4: Ready, Retire and Discover; stalled downloads; the Monday Discover nudge; the pages."""
import json

import pytest

from bingestats.services import pin_lists, pin_notify, pins
from bingestats.worker import downloads
from tests.test_pins import ALEX, NOW, OWNER, D, library


def play(c, user, show_guid, show_rk, ep_guid, t, watched=1):
    c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, title, guid, "
              "grandparent_guid, grandparent_rating_key, watched) VALUES (1, ?, ?, ?, 2400, 'episode', 'x', ?, ?, ?, ?)",
              (user, t, t + 2400, ep_guid, show_guid, show_rk, watched))


def build(c):
    library(c)
    # Pinned Show (tvdb 111, rk 10): E1 (101) is in Plex already; add E2, E3. Marc has watched E1.
    c.execute("UPDATE media_items SET guid = 'plex://show/p' WHERE rating_key = 10")
    c.execute("UPDATE media_items SET guid = 'plex://ep/1', added_at = ? WHERE rating_key = 101", (int(NOW) - 20 * D,))
    for rk, e, added in ((102, 2, NOW - 3 * D), (103, 3, NOW - 2 * D)):
        c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, grandparent_rating_key, season_index, "
                  "episode_index, added_at) VALUES (?, 1, 'episode', ?, ?, 10, 1, ?, ?)", (rk, f"plex://ep/{e}", f"Ep {e}", e, int(added)))
    play(c, OWNER, "plex://show/p", 10, "plex://ep/1", int(NOW) - 10 * D)
    # Done And Dusted (tvdb 777): ended, both episodes watched by Marc
    c.execute("INSERT INTO sonarr_series (id, title, tvdb_id, status, previous_airing) VALUES (7, 'Done And Dusted', 777, 'ended', ?)",
              (int(NOW) - 400 * D,))
    c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, tvdb_id) VALUES (70, 1, 'show', 'plex://show/d', 'Done And Dusted', 777)")
    for rk, e in ((701, 1), (702, 2)):
        c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, grandparent_rating_key, season_index, "
                  "episode_index, added_at) VALUES (?, 1, 'episode', ?, 'x', 70, 1, ?, 1)", (rk, f"plex://dd/{e}", e))
        play(c, OWNER, "plex://show/d", 70, f"plex://dd/{e}", int(NOW) - 300 * D)
    # Unpinned (tvdb 333, rk 30): Marc watched it; Sonarr has its next episode tomorrow
    c.execute("UPDATE media_items SET guid = 'plex://show/u' WHERE rating_key = 30")
    for n in range(3):
        play(c, OWNER, "plex://show/u", 30, f"plex://u/{n}", int(NOW) - 100 * D + n)
    for t in (111, 222, 777):
        pins.pin(c, OWNER, t, now=NOW - 200 * D)
    return c


@pytest.fixture
def lib(conn):
    return build(conn)


def test_ready(lib):
    [r] = pin_lists.ready(lib, OWNER, NOW)
    assert (r["show"], r["next_code"], r["next_title"], r["waiting"], r["started"], r["new"]) == \
        ("Pinned Show", "S01E02", "Ep 2", 2, True, True)
    assert r["next_rating_key"] == 102
    pins.pin(lib, ALEX, 111)
    [a] = pin_lists.ready(lib, ALEX, NOW)                                  # never started: from the top
    assert (a["next_code"], a["waiting"], a["started"]) == ("S01E01", 3, False)


def test_retire_groups_undo(lib):
    groups = {g["key"]: [s["title"] for s in g["shows"]] for g in pin_lists.retire(lib, OWNER, NOW)}
    assert groups == {"finished": ["Done And Dusted"]}      # Pinned Show was watched 10 days ago; Sonarr Only isn't in Plex
    lib.execute("UPDATE pins SET notify = 0 WHERE tvdb_id = 777")
    assert pin_lists.retire_group(lib, OWNER, "finished", now=NOW) == 1
    assert pins.pinned(lib, OWNER) == {111, 222}
    assert pin_lists.last_retired(lib, OWNER)["count"] == 1
    assert pin_lists.retire_group(lib, OWNER, "nonsense", now=NOW) == 0
    assert pin_lists.undo_retire(lib, OWNER) == 1
    row = lib.execute("SELECT pinned_at, notify FROM pins WHERE user_id = ? AND tvdb_id = 777", (OWNER,)).fetchone()
    assert tuple(row) == (int(NOW - 200 * D), 0)                           # put back as it was
    assert pin_lists.last_retired(lib, OWNER) is None and pin_lists.undo_retire(lib, OWNER) == 0


def test_gone_cold_means_stopped_part_way(lib):
    lib.execute("UPDATE plays SET started_at = started_at - 400 * 86400 WHERE grandparent_rating_key = 10")
    [g] = [g for g in pin_lists.retire(lib, OWNER, NOW) if g["key"] == "cold"]
    assert [(s["title"], s["waiting"]) for s in g["shows"]] == [("Pinned Show", 2)]
    for t in (102, 103):                                                    # catch up: up to date and waiting
        lib.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, title, guid, "
                    "grandparent_guid, grandparent_rating_key, watched) VALUES (1, ?, ?, ?, 60, 'episode', 'x', ?, "
                    "'plex://show/p', 10, 1)", (OWNER, int(NOW) - 400 * D, int(NOW) - 400 * D + 60, f"plex://ep/{t - 100}"))
    assert all(g["key"] != "cold" for g in pin_lists.retire(lib, OWNER, NOW))   # for a new season is what pins are for
    pins.pin(lib, ALEX, 111, now=NOW - 5 * D)
    assert pin_lists.retire(lib, ALEX, NOW) == []                           # pinned recently, never played: not yet


def test_discover(lib):
    [s] = pin_lists.discover(lib, OWNER, NOW)
    assert (s["title"], s["outlook"], s["plays"], s["hours"]) == ("Unpinned", "dated", 3, 2.0)
    pins.pin(lib, OWNER, 333)
    assert pin_lists.discover(lib, OWNER, NOW) == []                       # pinned now
    assert pin_lists.discover(lib, ALEX, NOW) == []                        # Alex never watched it


def test_counts(lib):
    assert pin_lists.counts(lib, OWNER, NOW) == {"ready": 1, "retire": 1, "discover": 1}


def test_discover_nudge_once(lib):
    [ev] = pin_notify.discover_events(lib, NOW)
    assert ev["pinner"]["id"] == OWNER and ev["shows"][0]["text"].startswith("Unpinned · back on Fri 6 Nov")
    assert pin_notify.discover_events(lib, NOW + 60) == []                 # told already


# -- downloads -----------------------------------------------------------------------------------

def test_mark_stalled():
    st = {}
    rows = lambda left, status="Downloading": [{"kind": "job", "nzo_id": "A", "mb_left": left, "status": status},  # noqa: E731
                                               {"kind": "job", "nzo_id": "B", "mb_left": 500, "status": "Queued"},
                                               {"kind": "other", "count": 3}]
    r = rows(100)
    downloads.mark_stalled(r, {}, st, 0)
    assert r[0]["stalled_s"] == 0
    r = rows(100)
    downloads.mark_stalled(r, {}, st, 31 * 60)
    assert r[0]["stalled_s"] == 31 * 60 and r[1]["stalled_s"] == 0       # queued jobs never stall
    r = rows(90)
    downloads.mark_stalled(r, {}, st, 32 * 60)
    assert r[0]["stalled_s"] == 0                                          # it moved
    r = rows(90)
    downloads.mark_stalled(r, {"paused": True}, st, 80 * 60)
    assert r[0]["stalled_s"] == 0                                          # paused queue: not stalled
    downloads.mark_stalled([], {}, st, 90 * 60)
    assert st["progress"] == {}                                            # gone jobs are forgotten


def test_stalled_on_the_calendar(lib):
    live = {"rows": [{"kind": "job", "nzo_id": "N1", "pct": 40, "stalled_s": 2400}], "pp": []}
    lib.execute("UPDATE arr_status SET info = ? WHERE app = 'sabnzbd'",
                (json.dumps({"live": live, "live_map": {"N1": [{"app": "sonarr", "tvdb_id": 111, "season": 1, "episode": 6}]}}),))
    assert pins.stalled(lib) == {(111, 1, 6)}
    [e] = [e for e in pins.calendar(lib, OWNER, now=NOW)["overdue"] if e["episode"] == 6]
    assert (e["state"], e["label"], e["stalled"]) == ("downloading", "Stalled", True)


# -- pages ---------------------------------------------------------------------------------------

@pytest.fixture
def site(logged_in, app_conn):
    from tests.test_arr import connect
    build(app_conn)
    connect(app_conn)
    return logged_in


def test_pages(site, app_conn):
    cal = site.get("/calendar").data.decode()
    assert 'href="/calendar/ready"' in cal and "Ready (1)" in cal and "Retire (1)" in cal and "Discover (1)" in cal
    ready = site.get("/calendar/ready").data.decode()
    assert "Next S01E02" in ready and "2 waiting" in ready and 'href="/item/102"' in ready
    retire = site.get("/calendar/retire").data.decode()
    assert "Finished" in retire and "Unpin it" in retire
    assert "Unpinned" in site.get("/calendar/discover").data.decode()
    r = site.post("/calendar/retire/finished")
    assert r.status_code == 302 and 777 not in pins.pinned(app_conn, OWNER)
    assert "Undo, pin it again" in site.get("/calendar/retire").data.decode()
    site.post("/calendar/retire-undo")
    assert 777 in pins.pinned(app_conn, OWNER)


def test_viewer_pages(app, site, app_conn):
    from bingestats.services import accounts
    from tests.conftest import Browser
    accounts.set_password(app_conn, ALEX, "alex-password")
    pins.pin(app_conn, ALEX, 111)
    b = Browser(app.test_client())
    b.get("/login")
    b.post("/login", {"username": "alexp", "password": "alex-password"})
    page = b.get("/me/calendar/ready").data.decode()
    assert "Start at S01E01" in page and "/item/" not in page and 'href="/me/calendar/retire"' in page
    assert b.get("/me/calendar/discover").status_code == 200
    assert b.post("/calendar/retire/finished").status_code == 403
