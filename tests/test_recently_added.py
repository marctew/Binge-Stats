import pytest

from bingestats.services import settings
from bingestats.worker.recently_added import check_recent, digest

T = 1_800_000_000


@pytest.fixture
def lib(conn):
    conn.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    for lid, name, t in ((4, "Films", "movie"), (5, "TV", "show"), (6, "Music", "artist")):
        conn.execute("INSERT INTO libraries (id, server_id, name, type, synced_at) VALUES (?, 1, ?, ?, ?)",
                     (lid, name, t, T - 86400))
    conn.execute("INSERT INTO media_items (rating_key, server_id, library_id, type, title, thumb) "
                 "VALUES (30, 1, 5, 'show', 'Slow Horses', '/library/metadata/30/thumb/1')")
    return conn


def add(conn, rk, typ, title, queued=T, lib=5, **kw):
    cols = {"rating_key": rk, "server_id": 1, "library_id": lib, "type": typ, "title": title, "added_at": queued, **kw}
    conn.execute(f"INSERT INTO media_items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(cols.values()))
    conn.execute("INSERT INTO recently_added_queue (rating_key, queued_at) VALUES (?, ?)", (rk, queued))


def ep(conn, rk, n, season=5, queued=T):
    add(conn, rk, "episode", f"Episode {n}", queued, grandparent_rating_key=30, grandparent_title="Slow Horses",
        season_index=season, episode_index=n, thumb=f"/library/metadata/{rk}/thumb/1")


def test_waits_until_queue_is_quiet(lib):
    ep(lib, 501, 1, queued=T)
    ep(lib, 502, 2, queued=T + 300)
    assert digest(lib, now=T + 400) == []                 # still arriving
    events = digest(lib, now=T + 300 + 600)
    assert len(events) == 1
    assert digest(lib, now=T + 2000) == []                # processed


def test_season_drop_is_one_event(lib):
    for n in (1, 2, 3):
        ep(lib, 500 + n, n)
    [e] = digest(lib, now=T + 3600)
    assert e["kind"] == "episode" and e["headline"] == "Slow Horses · Season 5 · 3 new episodes"
    assert e["poster"] == "/library/metadata/30/thumb/1"  # the show's poster, not an episode still
    assert [i["display_title"] for i in e["items"]] == [f"Slow Horses · S05E0{n} · Episode {n}" for n in (1, 2, 3)]


def test_single_episode_headline(lib):
    ep(lib, 501, 4)
    [e] = digest(lib, now=T + 3600)
    assert e["headline"] == "Slow Horses · S05E04 · Episode 4"


def test_films_batch_and_albums(lib):
    add(lib, 900, "movie", "Aftersun", lib=4, year=2022, thumb="/a")
    add(lib, 901, "movie", "Past Lives", lib=4, year=2023)
    add(lib, 950, "album", "Rumours", lib=6, parent_title="Fleetwood Mac", year=1977)
    events = {e["kind"]: e for e in digest(lib, now=T + 3600)}
    assert events["movie"]["headline"] == "2 new films" and events["movie"]["poster"] == "/a"
    assert [i["display_title"] for i in events["movie"]["items"]] == ["Aftersun (2022)", "Past Lives (2023)"]
    assert events["album"]["headline"] == "Fleetwood Mac · Rumours"


def test_two_seasons_two_events(lib):
    ep(lib, 501, 1, season=4)
    ep(lib, 502, 1, season=5)
    assert sorted(e["season"] for e in digest(lib, now=T + 3600)) == [4, 5]


def test_wait_setting(lib):
    settings.set(lib, "notify.digest_wait_min", 0)
    ep(lib, 501, 1)
    assert len(digest(lib, now=T)) == 1


def test_check_recent_queues_new_items(lib, plex):
    plex.library = {(5, 4): [
        {"ratingKey": "601", "type": "episode", "title": "New one", "grandparentRatingKey": "30",
         "grandparentTitle": "Slow Horses", "parentIndex": 6, "index": 1, "addedAt": T + 10}],
        (4, 1): [{"ratingKey": "910", "type": "movie", "title": "Dune", "year": 2024, "addedAt": T + 20}]}
    n = check_recent(lib, plex, 1, now=T + 30)
    assert n == 2
    assert {r[0] for r in lib.execute("SELECT rating_key FROM recently_added_queue")} == {601, 910}
    assert check_recent(lib, plex, 1, now=T + 60) == 0            # already known
    assert settings.get(lib, "library.last_recent_check_at") == T + 60


def test_check_recent_skips_never_synced_library(lib, plex):
    lib.execute("UPDATE libraries SET synced_at = NULL WHERE id = 4")
    plex.library = {(4, 1): [{"ratingKey": "910", "type": "movie", "title": "Dune", "addedAt": T}]}
    assert check_recent(lib, plex, 1, now=T) == 0


def test_digest_events_build_contexts(lib):
    from bingestats.services import notify
    for n in (1, 2):
        ep(lib, 500 + n, n)
    [e] = digest(lib, now=T + 3600)
    ctx = notify.build_context(lib, "recently_added", e)
    assert ctx["count"] == 2 and ctx["library"]["name"] == "TV" and ctx["kind_label"] == "TV episodes"
    default = next(r for r in notify.list_rules(lib) if r["trigger"] == "recently_added")
    title = notify.render(default["title_template"], ctx, 250)
    assert title == "New on Plex: Slow Horses · Season 5 · 2 new episodes"
