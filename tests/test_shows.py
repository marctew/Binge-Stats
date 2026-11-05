"""The shows browser: filters and their counts, sorting, paging, bulk pin and undo, and the pages."""
import json

import pytest
from werkzeug.datastructures import MultiDict

from bingestats.services import pins
from bingestats.services import shows as sv

NOW = 1_793_900_000
D = 86400
OWNER, ALEX = 1, 11


def build(c):
    c.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    c.execute("INSERT INTO users (id, username, is_owner, share_salt) VALUES (1, 'marc', 1, 's')")
    c.execute("INSERT INTO users (id, username, share_salt) VALUES (11, 'alexp', 's')")
    c.execute("INSERT INTO libraries (id, server_id, name, type) VALUES (5, 1, 'TV', 'show'), (6, 1, 'Anime', 'show')")
    # (tvdb, title, library, sonarr status, network, genres, next airing, previous airing)
    data = [(1, "Andor", 5, "ended", "Disney+", ["Drama", "Sci-Fi"], None, NOW - 300 * D),
            (2, "Bluey", 5, "continuing", "BBC One", ["Kids"], NOW + 5 * D, NOW - 2 * D),
            (3, "Chainsaw Man", 6, "continuing", "MBS", ["Animation", "Action"], NOW + 20 * D, NOW - 90 * D),
            (4, "Dark", 5, "ended", "Netflix", ["Drama", "Sci-Fi"], None, NOW - 2000 * D),
            (5, "Expanse", None, "continuing", "Prime Video", ["Sci-Fi"], None, NOW - 900 * D)]   # Sonarr only
    for tvdb, title, lib, status, net, genres, nxt, prev in data:
        c.execute("INSERT INTO sonarr_series (id, title, tvdb_id, status, network, genres, next_airing, previous_airing) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (tvdb, title, tvdb, status, net, json.dumps(genres), nxt, prev))
        if lib:
            c.execute("INSERT INTO media_items (rating_key, server_id, library_id, type, guid, title, tvdb_id) "
                      "VALUES (?, 1, ?, 'show', ?, ?, ?)", (tvdb * 10, lib, f"plex://show/{tvdb}", title, tvdb))
    c.execute("INSERT INTO tmdb_shows (tmdb_id, tvdb_id, status, genres, fetched_at) VALUES (40, 4, 'Canceled', '[\"Mystery\"]', 1)")
    for tvdb, t in ((4, NOW - 5 * D), (1, NOW - D)):                 # Dark, then Andor, watched by the owner
        c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, title, "
                  "grandparent_guid, grandparent_rating_key) VALUES (1, 1, ?, ?, 60, 'episode', 'x', ?, ?)",
                  (t, t + 60, f"plex://show/{tvdb}", tvdb * 10))
    return c


@pytest.fixture
def lib(conn):
    return build(conn)


def view(c, user=OWNER, **args):
    return sv.browse(c, user, sv.Filter.from_args(MultiDict(args)), now=NOW)


def titles(d):
    return [s["title"] for s in d["items"]]


def counts(d, facet):
    return {v["value"]: v["count"] for f in d["facets"] if f["name"] == facet for v in f["options"]}


def test_default_view_is_recently_watched_first(lib):
    d = view(lib)
    assert titles(d) == ["Andor", "Dark", "Bluey", "Chainsaw Man", "Expanse"]
    assert d["total"] == d["all"] == 5 and d["pages"] == 1


def test_sorts(lib):
    assert titles(view(lib, sort="title"))[0] == "Andor"
    assert titles(view(lib, sort="next"))[:2] == ["Bluey", "Chainsaw Man"]
    assert titles(view(lib, sort="outlook"))[:2] == ["Bluey", "Chainsaw Man"]      # dated first


def test_facets_and_or_within_one(lib):
    assert titles(view(lib, genre="Sci-Fi", sort="title")) == ["Andor", "Dark", "Expanse"]
    assert titles(view(lib, genre=["Sci-Fi", "Kids"], sort="title")) == ["Andor", "Bluey", "Dark", "Expanse"]
    assert titles(view(lib, genre="Sci-Fi", status="Ended", sort="title")) == ["Andor", "Dark"]
    assert titles(view(lib, library="Not in Plex")) == ["Expanse"]
    assert titles(view(lib, q="chain")) == ["Chainsaw Man"]
    assert titles(view(lib, genre="Mystery")) == ["Dark"]                         # TMDB's genres count too


def test_counts_leave_their_own_facet_out(lib):
    d = view(lib, genre="Sci-Fi")
    assert counts(d, "genre")["Kids"] == 1                 # what you'd add by ticking it, not zero
    assert counts(d, "status") == {"Ended": 2, "Continuing": 1}
    assert counts(d, "library") == {"TV": 2, "Not in Plex": 1}
    assert counts(d, "outlook") == {"cancelled": 1, "ended": 1, "dormant": 1}
    assert counts(view(lib, network="Nobody"), "network")["Nobody"] == 0           # a ticked value stays listed


def test_paging(lib, monkeypatch):
    monkeypatch.setattr(sv, "PAGE", 2)
    d = view(lib, sort="title", page="3")
    assert titles(d) == ["Expanse"] and d["pages"] == 3
    assert view(lib, page="99")["page"] == 3 and view(lib, page="x")["page"] == 1


def test_query_round_trip():
    f = sv.Filter.from_args(MultiDict([("genre", "Drama"), ("genre", "Sci-Fi"), ("sort", "title"), ("page", "2")]))
    assert f.query() == {"genre": "Drama,Sci-Fi", "sort": "title", "page": 2}
    assert sv.Filter.from_args(f.query()).picked["genre"] == {"Drama", "Sci-Fi"}
    assert f.toggled("genre", "Drama") == {"genre": "Sci-Fi", "sort": "title"}


def test_pin_all_reruns_the_filter_and_undo(lib):
    pins.pin(lib, OWNER, 1)
    f = sv.Filter.from_args(MultiDict({"genre": "Sci-Fi"}))
    assert sv.pin_all(lib, OWNER, f, now=NOW) == 2                     # Andor was pinned already
    assert pins.pinned(lib, OWNER) == {1, 4, 5}
    assert sv.last_batch(lib, OWNER) == {"batch": f"bulk-{NOW}", "count": 2}
    assert sv.undo_last(lib, OWNER) == 2 and pins.pinned(lib, OWNER) == {1}      # the hand-made pin stays
    assert sv.undo_last(lib, OWNER) == 0 and pins.pinned(lib, ALEX) == set()


def test_pinned_facet_is_per_person(lib):
    pins.pin(lib, ALEX, 2)
    assert titles(view(lib, user=ALEX, pinned="yes")) == ["Bluey"]
    assert view(lib, pinned="yes")["total"] == 0


# -- pages ------------------------------------------------------------------------------------

@pytest.fixture
def site(logged_in, app_conn):
    from tests.test_arr import connect
    build(app_conn)
    connect(app_conn)
    return logged_in


def test_shows_page(site, app_conn):
    page = site.get("/shows").data.decode()
    assert 'href="/shows"' in page and "5 shows" in page and "Andor" in page and "Sci-Fi" in page
    assert 'action="/pins/1"' in page and 'href="/item/10"' in page
    assert "Pin all" not in page                                   # only once something is filtered
    page = site.get("/shows?genre=Sci-Fi").data.decode()
    assert "Pin all 3" in page and "Bluey" not in page
    r = site.post("/shows/pin-all", {"genre": "Sci-Fi"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/shows?genre=Sci-Fi")
    assert pins.pinned(app_conn, OWNER) == {1, 4, 5}
    assert "Undo last bulk pin (3)" in site.get("/shows?genre=Sci-Fi").data.decode()
    site.post("/shows/undo", {"genre": "Sci-Fi"})
    assert pins.pinned(app_conn, OWNER) == set()
    assert "No shows match" in site.get("/shows?q=zzz").data.decode()


def test_viewer_shows_page(app, site, app_conn):
    from bingestats.services import accounts
    from tests.conftest import Browser
    accounts.set_password(app_conn, ALEX, "alex-password")
    b = Browser(app.test_client())
    b.get("/login")
    b.post("/login", {"username": "alexp", "password": "alex-password"})
    page = b.get("/me/shows").data.decode()
    assert 'href="/me/shows"' in page and "Andor" in page and "/item/" not in page
    b.get("/me/shows?genre=Kids")
    assert b.post("/me/shows/pin-all", {"genre": "Kids"}).status_code == 302
    assert pins.pinned(app_conn, ALEX) == {2} and pins.pinned(app_conn, OWNER) == set()
    assert b.post("/shows/pin-all", {"genre": "Kids"}).status_code == 403
