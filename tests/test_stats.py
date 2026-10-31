"""Stats correctness on small hand-built histories (Europe/London)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bingestats.services import stats

LON = ZoneInfo("Europe/London")


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=LON).timestamp())


def add(conn, started, play_s, *, user=1, mt="episode", group=None, decision="direct_play", show="Slow Horses",
        title="Ep", guid=None, gguid="plex://show/sh", product="Plex for LG", local=1, src="1080", dst="1080", pid=None):
    cur = conn.execute(
        "INSERT INTO plays (id, server_id, group_id, user_id, started_at, stopped_at, play_s, media_type, decision, "
        "grandparent_title, grandparent_guid, title, guid, product, player_title, is_local, src_resolution, "
        "dst_resolution, library_id) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'TV', ?, ?, ?, 5)",
        (pid, group, user, started, started + play_s, play_s, mt, decision, show if mt == "episode" else None,
         gguid if mt == "episode" else None, title, guid, product, local, src, dst))
    return cur.lastrowid


@pytest.fixture
def db(conn):
    conn.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (1, 'sarah', 'Sarah', 's'), "
                 "(2, 'marc', NULL, 's')")
    return conn


NOW = ts(2026, 10, 31, 12)


def rng(db, **kw):
    return stats.make_range(db, now=NOW, **kw)


def test_range_presets_are_local_days(db):
    r = rng(db, preset="7d")
    assert r.start == ts(2026, 10, 25) and r.end == ts(2026, 11, 1)
    assert r.days == 7 and r.auto_group() == "day"
    assert rng(db, preset="1y").auto_group() == "week"
    r = rng(db, date_from="2026-10-01", date_to="2026-10-03")
    assert (r.start, r.end) == (ts(2026, 10, 1), ts(2026, 10, 4))
    assert rng(db, date_from="nonsense").label == "Last 30 days"


def test_summary_counts_resumes_once_but_sums_time(db):
    head = add(db, ts(2026, 10, 30, 20), 1200)
    add(db, ts(2026, 10, 30, 21), 1500, group=head)
    add(db, ts(2026, 10, 29, 20), 600, user=2, mt="movie", title="Aftersun", decision="transcode", local=0)
    s = stats.summary(db, rng(db, preset="7d"))
    assert s["plays"] == 2 and s["watch_time_s"] == 3300
    assert s["users"] == 2 and s["titles"] == 2
    assert s["transcode_pct"] == 50 and s["remote_pct"] == 50


def test_daily_series_across_clocks_change(db):
    # UK clocks went back at 02:00 BST on Sun 25 Oct 2026. 00:30 and 23:30 that
    # day are the same local date even though the day is 25 hours long.
    add(db, ts(2026, 10, 25, 0, 30), 600)
    add(db, ts(2026, 10, 25, 23, 30), 600)
    add(db, ts(2026, 10, 26, 0, 10), 600)
    t = stats.timeseries(db, rng(db, preset="7d"), group="day")
    assert t["labels"][0] == "25 Oct"
    assert t["series"][0]["data"][:2] == [2, 1]


def test_split_by_media_type_and_watch_time_hours(db):
    add(db, ts(2026, 10, 30, 20), 3600)
    add(db, ts(2026, 10, 30, 21), 5400, mt="movie", title="Dune")
    t = stats.timeseries(db, rng(db, preset="7d"), metric="watch_time", split="media_type")
    by = {s["name"]: s["data"] for s in t["series"]}
    assert by["TV"][5] == 1.0 and by["Films"][5] == 1.5


def test_split_by_user_uses_display_names(db):
    add(db, ts(2026, 10, 30, 20), 600, user=1)
    add(db, ts(2026, 10, 30, 21), 600, user=2)
    names = {s["name"] for s in stats.timeseries(db, rng(db, preset="7d"), split="user")["series"]}
    assert names == {"Sarah", "marc"}


def test_weekly_and_monthly_buckets(db):
    add(db, ts(2026, 10, 5, 20), 600)     # Mon
    add(db, ts(2026, 10, 11, 20), 600)    # Sun same week
    add(db, ts(2026, 9, 15, 20), 600)
    w = stats.timeseries(db, rng(db, date_from="2026-09-01", date_to="2026-10-31"), group="week")
    assert dict(zip(w["labels"], w["series"][0]["data"]))["05 Oct"] == 2
    m = stats.timeseries(db, rng(db, date_from="2026-09-01", date_to="2026-10-31"), group="month")
    assert m["labels"] == ["Sep 2026", "Oct 2026"] and m["series"][0]["data"] == [1, 2]


def test_too_many_series_fold_into_other(db):
    for u in range(10, 22):
        db.execute("INSERT INTO users (id, username, share_salt) VALUES (?, ?, 's')", (u, f"u{u}"))
        add(db, ts(2026, 10, 30, 20), 600, user=u)
    names = [s["name"] for s in stats.timeseries(db, rng(db, preset="7d"), split="user")["series"]]
    assert len(names) == stats.MAX_SERIES and names[-1] == "Other"


def test_heatmap_spreads_watch_time_across_hours(db):
    add(db, ts(2026, 10, 30, 20, 30), 2 * 3600)   # Fri 20:30-22:30
    h = stats.heatmap(db, rng(db, preset="7d"))
    cells = {(x, y): v for x, y, v in h["data"] if v}
    assert cells == {(20, 4): 0.5, (21, 4): 1.0, (22, 4): 0.5}
    p = stats.heatmap(db, rng(db, preset="7d"), metric="plays")
    assert [c for c in p["data"] if c[2]] == [[20, 4, 1]]


def test_by_period(db):
    add(db, ts(2026, 10, 30, 20), 600)    # Fri 20h
    add(db, ts(2026, 10, 30, 21), 600)
    assert stats.by_period(db, rng(db, preset="7d"), dim="weekday")["series"][0]["data"][4] == 2
    hours = stats.by_period(db, rng(db, preset="7d"), dim="hour")["series"][0]["data"]
    assert hours[20] == 1 and hours[21] == 1


def test_concurrent_peak(db):
    add(db, ts(2026, 10, 30, 20), 3600)
    add(db, ts(2026, 10, 30, 20, 30), 3600)
    add(db, ts(2026, 10, 30, 21), 600)        # starts exactly as the first ends
    peak, at = stats.concurrent_peak(db, rng(db, preset="7d"))
    assert peak == 2 and at == ts(2026, 10, 30, 20, 30)
    series = stats.concurrent_series(db, rng(db, preset="7d"))["series"][0]["data"]
    assert series[5] == 2


def test_breakdowns(db):
    add(db, ts(2026, 10, 30, 20), 600, product="Plex for LG")
    add(db, ts(2026, 10, 30, 21), 600, product="Plex for LG")
    add(db, ts(2026, 10, 30, 22), 600, product="Plex Web", decision="transcode", src="4k", dst="1080")
    b = stats.breakdown(db, rng(db, preset="7d"), field="product")
    assert b["items"][0] == {"name": "Plex for LG", "value": 2}
    d = {i["name"]: i["value"] for i in stats.breakdown(db, rng(db, preset="7d"), field="decision")["items"]}
    assert d == {"Direct Play": 2, "Transcode": 1}
    res = [i["name"] for i in stats.breakdown(db, rng(db, preset="7d"), field="src_resolution")["items"]]
    assert res == ["4K", "1080p"]
    flow = stats.resolution_flow(db, rng(db, preset="7d"))
    assert flow["labels"] == ["4K", "1080p"]
    assert {s["name"]: s["data"] for s in flow["series"]}["Played as 1080p"] == [1, 2]


def test_top_lists(db):
    add(db, ts(2026, 10, 30, 20), 600, show="Slow Horses", gguid="plex://show/sh")
    add(db, ts(2026, 10, 30, 21), 600, show="Slow Horses", gguid="plex://show/sh", user=2)
    add(db, ts(2026, 10, 30, 22), 3000, show="Bluey", gguid="plex://show/bl")
    add(db, ts(2026, 10, 29, 20), 7000, mt="movie", title="Dune", guid="plex://movie/d")
    shows = stats.top(db, rng(db, preset="7d"), kind="shows")["items"]
    assert [(s["name"], s["plays"], s["users"]) for s in shows] == [("Slow Horses", 2, 2), ("Bluey", 1, 1)]
    by_time = stats.top(db, rng(db, preset="7d"), kind="shows", metric="watch_time")["items"]
    assert by_time[0]["name"] == "Bluey"
    users = stats.top(db, rng(db, preset="7d"), kind="users", metric="watch_time")["items"]
    assert users[0]["name"] == "Sarah" and users[0]["play_s"] == 600 + 3000 + 7000
    assert stats.top(db, rng(db, preset="7d"), kind="movies")["items"][0]["name"] == "Dune"


def test_scope_by_user_and_library(db):
    add(db, ts(2026, 10, 30, 20), 600, user=1)
    add(db, ts(2026, 10, 30, 21), 600, user=2)
    assert stats.summary(db, rng(db, preset="7d"), user_id=2)["plays"] == 1
    assert stats.summary(db, rng(db, preset="7d"), library_id=99)["plays"] == 0


def test_empty_range_is_all_zero(db):
    s = stats.summary(db, rng(db, preset="7d"))
    assert s["plays"] == 0 and s["peak_concurrent"] == 0 and s["transcode_pct"] == 0
    t = stats.timeseries(db, rng(db, preset="7d"))
    assert t["series"] == [] and len(t["labels"]) == 7
    assert stats.heatmap(db, rng(db, preset="7d"))["max"] == 0


def test_recently_added_groups_episodes_by_show(db):
    db.execute("INSERT INTO media_items (rating_key, server_id, type, title, thumb, added_at) VALUES "
               "(30, 1, 'show', 'Slow Horses', '/s', 100)")
    for rk in (501, 502):
        db.execute("INSERT INTO media_items (rating_key, server_id, type, title, grandparent_rating_key, "
                   "grandparent_title, added_at) VALUES (?, 1, 'episode', 'E', 30, 'Slow Horses', ?)", (rk, rk))
    db.execute("INSERT INTO media_items (rating_key, server_id, type, title, thumb, added_at) VALUES "
               "(900, 1, 'movie', 'Dune', '/d', 400)")
    items = stats.recently_added(db)
    assert [(i["title"], i["n"]) for i in items] == [("Slow Horses", 2), ("Dune", 1)]
    assert items[0]["thumb"] == "/s"


def test_series_colour_slots_follow_entity(db):
    add(db, ts(2026, 10, 30, 20), 600, decision="transcode")
    add(db, ts(2026, 10, 30, 21), 600, decision="direct_play")
    s = {x["name"]: x["slot"] for x in stats.timeseries(db, rng(db, preset="7d"), split="decision")["series"]}
    assert s == {"Transcode": 1, "Direct Play": 2}
    add(db, ts(2020, 1, 1), 600, user=2)
    add(db, ts(2020, 1, 2), 600, user=2)
    add(db, ts(2020, 1, 3), 600, user=2)      # marc has more plays all-time
    week = {x["name"]: x["slot"] for x in stats.timeseries(db, rng(db, preset="7d"), split="user")["series"]}
    assert week == {"Sarah": 1}               # Sarah keeps slot 1 even when she's alone in the range
