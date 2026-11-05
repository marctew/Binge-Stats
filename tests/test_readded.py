"""Things Plex re-added under a new rating key: top lists show today's artwork and link to
today's page, and old links (from history) still open the right item."""
import pytest

from bingestats.services import libraries, stats

T = 1_790_000_000


def play(c, *, rk, title, media_type, guid, thumb, gk=None, gtitle=None, gguid=None, t=T, play_s=3600):
    c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, rating_key, title, "
              "guid, thumb, grandparent_rating_key, grandparent_title, grandparent_guid, library_id, ip_address, "
              "player_title) VALUES (1, 7, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '10.0.0.2', 'TV')",
              (t, t + play_s, play_s, media_type, rk, title, guid, thumb, gk, gtitle, gguid,
               5 if media_type == "episode" else None))


@pytest.fixture
def lib(conn):
    c = conn
    c.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    c.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (7, 'marc', 'Marc', 's')")
    c.execute("INSERT INTO libraries (id, server_id, name, type) VALUES (5, 1, 'TV', 'show')")
    # Suits: every play is from before Plex re-added it (old key 5284, now 83578)
    c.execute("INSERT INTO media_items (rating_key, server_id, library_id, type, guid, title, thumb) VALUES "
              "(83578, 1, 5, 'show', 'plex://show/suits', 'Suits', '/library/metadata/83578/thumb/2')")
    for i in range(3):
        play(c, rk=6000 + i, title=f"Ep {i}", media_type="episode", guid=f"plex://episode/s{i}",
             thumb="/library/metadata/5284/thumb/1", gk=5284, gtitle="Suits", gguid="plex://show/suits")
    # Kingsman: plays under both keys; the old path sorts after the new one as text
    c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, thumb) VALUES "
              "(104348, 1, 'movie', 'plex://movie/k', 'Kingsman', '/library/metadata/104348/thumb/9')")
    play(c, rk=4760, title="Kingsman", media_type="movie", guid="plex://movie/k", thumb="/library/metadata/4760/thumb/1")
    play(c, rk=104348, title="Kingsman", media_type="movie", guid="plex://movie/k",
         thumb="/library/metadata/104348/thumb/9", t=T + 10)
    # Gone from Plex altogether: keeps what the plays say
    play(c, rk=555, title="Gone Film", media_type="movie", guid="plex://movie/gone", thumb="/library/metadata/555/thumb/1")
    return c


def by_name(items, key="name"):
    return {i[key]: i for i in items}


def test_top_lists_follow_the_guid(lib):
    rng = stats.make_range(lib, preset="all")
    shows = by_name(stats.top(lib, rng, kind="shows")["items"])
    assert shows["Suits"]["id"] == 83578 and shows["Suits"]["thumb"] == "/library/metadata/83578/thumb/2"
    films = by_name(stats.top(lib, rng, kind="movies")["items"])
    assert films["Kingsman"]["id"] == 104348 and films["Kingsman"]["thumb"] == "/library/metadata/104348/thumb/9"
    assert films["Gone Film"]["id"] == 555 and films["Gone Film"]["thumb"] == "/library/metadata/555/thumb/1"


def test_user_page_and_library_lists_follow_the_guid(lib):
    u = libraries.get_user(lib, 7)
    assert by_name(u["top_shows"], "title")["Suits"]["thumb"] == "/library/metadata/83578/thumb/2"
    assert by_name(u["top_movies"], "title")["Kingsman"]["rating_key"] == 104348
    lp = by_name(libraries.get_library(lib, 5)["most_played"], "title")
    assert lp["Suits"]["rating_key"] == 83578


def test_old_keys_lead_to_the_new_item(lib):
    assert libraries.moved_to(lib, 5284) == 83578          # a show, known only from plays
    assert libraries.moved_to(lib, 4760) == 104348         # a film
    assert libraries.moved_to(lib, 83578) is None          # already current
    assert libraries.moved_to(lib, 555) is None            # gone for good
    lib.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, is_present) VALUES "
                "(4000, 1, 'movie', 'plex://movie/k', 'Kingsman', 0)")
    assert libraries.moved_to(lib, 4000) == 104348         # an old library row, marked gone


def test_item_page_redirects(logged_in, app_conn):
    from tests.conftest import Browser  # noqa: F401  (logged_in is a Browser)
    app_conn.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    app_conn.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (7, 'marc', 'Marc', 's')")
    app_conn.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title) VALUES "
                     "(83578, 1, 'show', 'plex://show/suits', 'Suits')")
    play(app_conn, rk=6000, title="Pilot", media_type="episode", guid="plex://episode/s0", thumb=None,
         gk=5284, gtitle="Suits", gguid="plex://show/suits")
    r = logged_in.get("/item/5284")
    assert r.status_code == 302 and r.headers["Location"].endswith("/item/83578")
    body = logged_in.get("/item/83578").data.decode()
    assert "Suits" in body and "No longer in Plex" not in body
