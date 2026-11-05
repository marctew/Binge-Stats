"""Season outlook: the order of the rules (ported from Pinnarr's tests), how Sonarr and TMDB feed
it, and where it shows (calendar groups, show pages)."""
import json

import pytest

from bingestats.services import outlook as ol
from bingestats.services import pins

NOW = 1_793_900_000
D = 86400


def at(days):
    return NOW + days * D


def c(**kw):
    return ol.compute(now=NOW, **kw)


# -- the rules, in order -------------------------------------------------------------------

def test_future_episode_wins():
    assert c(next_at=at(3)) == ol.DATED


def test_past_next_airing_does_not_count_as_dated():
    assert c(next_at=at(-5), previous_at=at(-5), sonarr_status="continuing") == ol.BETWEEN_SEASONS


def test_dated_beats_a_stale_ended_status():
    assert c(next_at=at(7), tmdb_status="Ended") == ol.DATED


def test_announced_when_a_later_season_has_not_aired():
    assert c(latest_season=3, latest_aired_season=2) == ol.ANNOUNCED
    assert c(latest_season=2, latest_aired_season=2) == ol.UNKNOWN


def test_in_production():
    assert c(in_production=True) == ol.IN_PRODUCTION
    assert c(tmdb_status="In Production") == ol.IN_PRODUCTION


def test_cancelled_and_ended_are_told_apart():
    assert c(tmdb_status="Canceled") == ol.CANCELLED
    assert c(tmdb_status="Ended") == ol.ENDED
    assert c(sonarr_status="ended") == ol.ENDED                      # Sonarr alone is enough for ended


def test_ended_outranks_hiatus():
    assert c(previous_at=at(-30), sonarr_status="continuing", tmdb_status="Ended") == ol.ENDED


def test_hiatus_and_dormant():
    assert c(previous_at=at(-60), sonarr_status="continuing") == ol.BETWEEN_SEASONS
    assert c(previous_at=at(-400), sonarr_status="continuing") == ol.BETWEEN_SEASONS   # long gap, not yet suspicious
    assert c(previous_at=at(-700), sonarr_status="continuing") == ol.DORMANT


def test_dormant_threshold_is_adjustable():
    assert c(previous_at=at(-730), dormant_months=36) == ol.BETWEEN_SEASONS
    assert c(previous_at=at(-730), dormant_months=12) == ol.DORMANT


def test_filming_beats_a_long_silence():
    assert c(previous_at=at(-900), in_production=True) == ol.IN_PRODUCTION


def test_never_aired_but_planned():
    assert c(tmdb_status="Planned") == ol.ANNOUNCED
    assert c(sonarr_status="upcoming") == ol.ANNOUNCED


def test_no_signal():
    assert c() == ol.UNKNOWN


# -- inputs from Sonarr and TMDB -----------------------------------------------------------

@pytest.fixture
def lib(conn):
    conn.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    conn.execute("INSERT INTO users (id, username, is_owner, share_salt) VALUES (1, 'marc', 1, 's')")

    def series(i, title, tvdb, *, status="continuing", nxt=None, prev=None, seasons=(1, 2)):
        conn.execute("INSERT INTO sonarr_series (id, title, tvdb_id, status, next_airing, previous_airing, seasons) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (i, title, tvdb, status, nxt, prev,
                                                     json.dumps([{"season": s} for s in (0, *seasons)])))

    def tmdb(tvdb, *, status="Returning Series", in_production=0, next_date=None, last_date=None,
             last_season=None, max_season=None):
        conn.execute("INSERT INTO tmdb_shows (tmdb_id, tvdb_id, status, in_production, next_air_date, last_air_date, "
                     "last_season, max_season, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                     (tvdb + 10000, tvdb, status, in_production, next_date, last_date, last_season, max_season))

    series(1, "Dated", 1, nxt=at(30), prev=at(-100))
    series(2, "Cancelled", 2, prev=at(-60)); tmdb(2, status="Canceled", last_season=2, max_season=2)
    series(3, "Filming", 3, prev=at(-800)); tmdb(3, in_production=1, last_season=2, max_season=2)
    series(4, "Season 3 Announced", 4, prev=at(-200), seasons=(1, 2, 3)); tmdb(4, last_season=2, max_season=3)
    series(5, "Gone Quiet", 5, prev=at(-700))
    series(6, "Unmonitored S3", 6, prev=at(-100), seasons=(1, 2, 3))              # no TMDB, not pinned
    series(7, "TMDB Has A Date", 7, prev=at(-300)); tmdb(7, next_date="2027-01-10", last_season=1, max_season=2)
    tmdb(8, status="Ended", last_date="2020-01-01")                              # TMDB only
    return conn


def test_for_shows(lib):
    got = {t: o["outlook"] for t, o in ol.for_shows(lib, now=NOW).items()}
    assert got == {1: ol.DATED, 2: ol.CANCELLED, 3: ol.IN_PRODUCTION, 4: ol.ANNOUNCED, 5: ol.DORMANT,
                   6: ol.BETWEEN_SEASONS, 7: ol.DATED, 8: ol.ENDED}
    assert ol.for_shows(lib, [7], now=NOW)[7]["next_at"] == 1799539200            # TMDB's date, at midnight UTC
    assert ol.for_shows(lib, [], now=NOW) == {}


def test_without_tmdb_a_pinned_shows_episodes_tell_what_has_aired(lib):
    lib.execute("INSERT INTO media_items (rating_key, server_id, type, title, tvdb_id) VALUES (60, 1, 'show', 'U', 6)")
    pins.pin(lib, 1, 6)
    lib.execute("INSERT INTO sonarr_episodes VALUES (6, 2, 1, 'x', ?, 1, 1)", (at(-100),))
    lib.execute("INSERT INTO sonarr_episodes VALUES (6, 3, 1, 'y', NULL, 0, 1)")  # season 3 listed, no date
    assert ol.for_shows(lib, [6], now=NOW)[6]["outlook"] == ol.ANNOUNCED


# -- where it shows ------------------------------------------------------------------------

def test_calendar_groups_shows_with_nothing_to_come(lib):
    for tvdb, title in ((2, "Cancelled"), (3, "Filming"), (5, "Gone Quiet"), (7, "TMDB Has A Date")):
        lib.execute("INSERT INTO media_items (rating_key, server_id, type, title, tvdb_id) VALUES (?, 1, 'show', ?, ?)",
                    (tvdb * 10, title, tvdb))
        pins.pin(lib, 1, tvdb)
    cal = pins.calendar(lib, 1, now=NOW)
    assert [(g["outlook"], [s["title"] for s in g["shows"]]) for g in cal["undated_groups"]] == [
        ("in_production", ["Filming"]), ("dormant", ["Gone Quiet"]), ("cancelled", ["Cancelled"])]
    assert [s["title"] for s in cal["later"]] == ["TMDB Has A Date"]               # a date from TMDB counts


def test_show_page_badge(logged_in, app_conn):
    app_conn.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    app_conn.execute("INSERT INTO users (id, username, is_owner, share_salt) VALUES (1, 'marc', 1, 's')")
    app_conn.execute("INSERT INTO media_items (rating_key, server_id, type, title, tvdb_id) VALUES (20, 1, 'show', 'C', 2)")
    app_conn.execute("INSERT INTO sonarr_series (id, title, tvdb_id, status) VALUES (2, 'C', 2, 'continuing')")
    app_conn.execute("INSERT INTO tmdb_shows (tmdb_id, tvdb_id, status, fetched_at) VALUES (9, 2, 'Canceled', 1)")
    body = logged_in.get("/item/20").data.decode()
    assert "Season outlook" in body and ">Cancelled</span>" in body
