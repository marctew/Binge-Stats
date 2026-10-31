"""Binges, completion and abandoned shows (SPEC.md section 11)."""
import random

import pytest

from bingestats import db
from bingestats.services import binges, completion, settings

T0 = 1_700_000_000
SHOWS = {"A": ("plex://show/a", 10), "B": ("plex://show/b", 20)}


@pytest.fixture(autouse=True)
def fresh_cache():
    completion.clear_cache()
    yield
    completion.clear_cache()


def setup_users(conn, *ids):
    for uid in ids:
        conn.execute("INSERT OR IGNORE INTO users (id, username, share_salt) VALUES (?, ?, 's')", (uid, f"u{uid}"))


def library(conn, show="A", episodes=6, specials=0, season=1):
    guid, rk = SHOWS[show]
    conn.execute("INSERT OR IGNORE INTO media_items (rating_key, server_id, type, guid, title) VALUES (?, 1, 'show', ?, ?)",
                 (rk, guid, f"Show {show}"))
    for e in range(1, episodes + 1):
        conn.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, grandparent_rating_key, "
                     "season_index, episode_index, size_bytes) VALUES (?, 1, 'episode', ?, ?, ?, ?, ?, 1000000000)",
                     (rk * 100 + e, f"{guid}/e{e}", f"Ep {e}", rk, season, e))
    for e in range(1, specials + 1):
        conn.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, grandparent_rating_key, "
                     "season_index, episode_index) VALUES (?, 1, 'episode', ?, 'Special', ?, 0, ?)",
                     (rk * 100 + 50 + e, f"{guid}/s{e}", rk, e))


def play(conn, user, show, ep, start, length=1800, watched=1, group_id=None, guid_known=True, media="episode"):
    guid, rk = SHOWS.get(show, (None, None))
    cur = conn.execute(
        "INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, guid, rating_key, "
        "grandparent_guid, grandparent_rating_key, grandparent_title, title, watched, group_id) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user, start, start + length, length, media, f"{guid}/e{ep}" if guid else f"plex://movie/{ep}",
         rk * 100 + ep if rk else 9000 + ep, guid if guid_known else None, rk, f"Show {show}" if guid else None,
         f"Ep {ep}", watched, group_id))
    return cur.lastrowid


def rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT user_id, grandparent_guid, started_at, ended_at, episode_count, play_s, first_play_id, last_play_id "
        "FROM binges ORDER BY user_id, started_at")]


# -- runs ------------------------------------------------------------------------------

def test_three_back_to_back_episodes_make_a_binge(conn):
    setup_users(conn, 1)
    t = T0
    for ep in (1, 2, 3):
        play(conn, 1, "A", ep, t)
        t += 1800 + 600           # 10 minute gap
    binges.rebuild(conn)
    [b] = rows(conn)
    assert b[1] == "plex://show/a" and b[4] == 3 and b[5] == 5400 and b[2] == T0


def test_gap_too_long_or_another_show_breaks_the_run(conn):
    setup_users(conn, 1)
    play(conn, 1, "A", 1, T0)
    play(conn, 1, "A", 2, T0 + 1800 + 31 * 60)          # 31 min gap > 30
    play(conn, 1, "A", 3, T0 + 2 * 3600)
    play(conn, 1, "B", 1, T0 + 3 * 3600)                # another show in between
    play(conn, 1, "A", 4, T0 + 4 * 3600)
    binges.rebuild(conn)
    assert rows(conn) == []


def test_movie_in_between_doesnt_break_and_resumes_count_once(conn):
    setup_users(conn, 1)
    first = play(conn, 1, "A", 1, T0, 900, watched=0)
    play(conn, 1, "A", 1, T0 + 1000, 900, group_id=first)   # resumed part of ep 1
    play(conn, 1, "X", 1, T0 + 2000, 600, media="movie")    # movie between episodes
    play(conn, 1, "A", 2, T0 + 2700)
    binges.rebuild(conn)
    assert rows(conn) == []                                 # only 2 distinct episodes
    play(conn, 1, "A", 3, T0 + 4600)
    binges.rebuild(conn)
    [b] = rows(conn)
    assert b[4] == 3 and b[6] == first


def test_missing_grandparent_guid_falls_back_to_show_rating_key(conn):
    setup_users(conn, 1)
    library(conn, "A")
    for i, ep in enumerate((1, 2, 3)):
        play(conn, 1, "A", ep, T0 + i * 2000, guid_known=(ep != 2))
    binges.rebuild(conn)
    [b] = rows(conn)
    assert b[1] == "plex://show/a" and b[4] == 3


def test_settings_change_the_definition(conn):
    setup_users(conn, 1)
    for i, ep in enumerate((1, 2)):
        play(conn, 1, "A", ep, T0 + i * 2000)
    binges.rebuild(conn)
    assert rows(conn) == []
    settings.set(conn, "binge.min_episodes", 2)
    binges.rebuild(conn)
    assert len(rows(conn)) == 1


def test_incremental_matches_rebuild(conn):
    """Random histories for three people across two shows: building binges a
    play at a time gives exactly what a rebuild gives."""
    rnd = random.Random(7)
    setup_users(conn, 1, 2, 3)
    library(conn, "A", 30)
    t = {1: T0, 2: T0, 3: T0}
    ep = {(u, s): 1 for u in (1, 2, 3) for s in "AB"}
    events = []
    for _ in range(300):
        u = rnd.choice((1, 2, 3))
        s = rnd.choice("AAAB")
        t[u] += rnd.choice((60, 300, 900, 1500, 2400, 7200, 86400))
        events.append((t[u], u, s, ep[(u, s)], rnd.random() < 0.15))
        t[u] += 1800
        ep[(u, s)] += 1
    events.sort()
    for start, u, s, e, no_guid in events:
        pid = play(conn, u, s, e, start, guid_known=not no_guid)
        binges.on_play(conn, pid)
    incremental = rows(conn)
    assert len(incremental) > 5
    binges.rebuild(conn)
    assert rows(conn) == incremental


def test_poller_records_binges_as_plays_finish(conn, plex, clock, tmp_path):
    from bingestats.worker.poller import Poller
    settings.set(conn, "binge.min_episodes", 2)
    p = Poller(conn, plex, clock=clock, data_dir=tmp_path)
    for i in range(2):
        plex.play("sessions_direct_play.json")
        m = plex.sessions_data["MediaContainer"]["Metadata"][0]
        m["Session"]["id"] = f"s{i}"
        m["ratingKey"] = str(70000 + i)
        m["guid"] = f"plex://episode/x{i}"
        p.poll_once()
        clock.advance(600)
        p.poll_once()
        plex.stop_all()
        for _ in range(3):
            clock.advance(5)
            p.poll_once()
    [b] = conn.execute("SELECT episode_count FROM binges").fetchall()
    assert b[0] == 2


def test_queries(conn):
    setup_users(conn, 1, 2)
    library(conn, "A")
    for u, n in ((1, 4), (2, 3)):
        for ep in range(1, n + 1):
            play(conn, u, "A", ep, T0 + u * 100000 + ep * 2000)
    binges.rebuild(conn)
    assert [b["episode_count"] for b in binges.longest(conn)] == [4, 3]
    assert binges.longest(conn)[0]["show_rating_key"] == 10
    [s] = binges.by_show(conn)
    assert s["binges"] == 2 and s["episodes"] == 7 and s["people"] == 2
    assert [x["user_id"] for x in binges.leaderboard(conn)] == [1, 2]
    assert binges.totals(conn, user_id=2)["longest"] == 3
    assert binges.totals(conn, start=T0 + 150000)["binges"] == 1


def test_migration_4_queues_rebuild_only_with_history(tmp_path, monkeypatch):
    for with_history in (False, True):
        c = db.connect(tmp_path / f"v3-{with_history}.db")
        monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:3])
        monkeypatch.setattr(db, "LATEST", 3)
        db.migrate(c)
        if with_history:
            setup_users(c, 1)
            play(c, 1, "A", 1, T0)
        monkeypatch.undo()
        assert db.migrate(c) == db.LATEST
        queued = c.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'rebuild_binges'").fetchone()[0]
        assert queued == int(with_history)


# -- completion -------------------------------------------------------------------------

def test_completion_and_abandoned(conn):
    setup_users(conn, 1, 2)
    library(conn, "A", episodes=4, specials=2)
    library(conn, "B", episodes=2)
    now = T0 + 200 * 86400
    for ep in (1, 2):                                     # half of A, long ago
        play(conn, 1, "A", ep, T0 + ep * 2000)
    play(conn, 1, "A", 3, T0 + 9000, watched=0)           # started, not finished
    for ep in (1, 2):                                     # all of B, recently
        play(conn, 1, "B", ep, now - 86400 + ep * 2000)
    play(conn, 2, "A", 1, now - 3600)                     # a quarter, recent
    rows_ = {a["title"]: a for a in completion.for_user(conn, 1, now)}
    assert (rows_["Show A"]["watched"], rows_["Show A"]["total"], rows_["Show A"]["pct"]) == (2, 4, 50.0)
    assert rows_["Show B"]["pct"] == 100
    assert [a["title"] for a in completion.abandoned(conn, 1, now)] == ["Show A"]
    s = completion.summary(conn, 1, now)
    assert [a["title"] for a in s["finished"]] == ["Show B"] and s["in_progress"] == []
    assert [a["title"] for a in s["abandoned"]] == ["Show A"]
    assert completion.abandoned(conn, 2, now) == []      # recent, so not abandoned
    who = completion.for_show(conn, 10, now)
    assert [(a["user_id"], a["pct"]) for a in who] == [(1, 50.0), (2, 25.0)]


def test_completion_ignores_episodes_gone_from_library_and_cache(conn):
    setup_users(conn, 1)
    library(conn, "A", episodes=2)
    play(conn, 1, "A", 1, T0)
    play(conn, 1, "A", 7, T0 + 2000)                      # episode no longer in the library
    [a] = completion.for_user(conn, 1, T0)
    assert (a["watched"], a["total"]) == (1, 2)
    play(conn, 1, "A", 2, T0 + 4000)
    assert completion.for_user(conn, 1, T0 + 60)[0]["watched"] == 1          # cached
    assert completion.for_user(conn, 1, T0 + completion.CACHE_S + 1)[0]["watched"] == 2


def test_readded_show_found_through_episode_guid(conn):
    """A show re-added to Plex gets new rating keys; plays imported before
    that still point at the old ones. The episode guid links them back."""
    from bingestats.services.libraries import backfill_play_guids
    setup_users(conn, 1)
    library(conn, "A", episodes=3)                       # show now has rating key 10
    for i, ep in enumerate((1, 2, 3)):
        conn.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, guid, "
                     "grandparent_rating_key, grandparent_title, watched) VALUES (1, 1, ?, ?, 1800, 'episode', ?, "
                     "31543, 'Show A', 1)", (T0 + i * 2000, T0 + i * 2000 + 1800, f"plex://show/a/e{ep}"))
    assert completion.for_user(conn, 1, T0) == []      # old rating key: can't place it
    assert backfill_play_guids(conn) == 3
    assert {r[0] for r in conn.execute("SELECT grandparent_guid FROM plays")} == {"plex://show/a"}
    completion.clear_cache()
    assert completion.for_user(conn, 1, T0)[0]["pct"] == 100
    binges.rebuild(conn)
    assert rows(conn)[0][1] == "plex://show/a"


def test_migration_5_backfills_and_queues_rebuild(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "v4.db")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:4])
    monkeypatch.setattr(db, "LATEST", 4)
    db.migrate(c)
    setup_users(c, 1)
    library(c, "A", episodes=1)
    c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, guid, "
              "grandparent_rating_key) VALUES (1, 1, 1, 2, 1, 'episode', 'plex://show/a/e1', 999)")
    monkeypatch.undo()
    assert db.migrate(c) == db.LATEST
    assert c.execute("SELECT grandparent_guid FROM plays").fetchone()[0] == "plex://show/a"
    assert c.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'rebuild_binges'").fetchone()[0] == 1


def test_legacy_agent_plays_matched_by_title_and_episode_number(conn):
    from bingestats.services.libraries import backfill_play_guids
    setup_users(conn, 1)
    library(conn, "A", episodes=2)
    conn.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title) VALUES "
                 "(60, 1, 'show', 'plex://show/dup1', 'Twin'), (61, 1, 'show', 'plex://show/dup2', 'twin')")
    for title, ep in (("show a", 1), ("Show A", 2), ("Twin", 1)):
        conn.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, guid, "
                     "grandparent_rating_key, grandparent_title, season_index, episode_index, watched) VALUES "
                     "(1, 1, 1, 2, 1, 'episode', ?, 31543, ?, 1, ?, 1)",
                     (f"com.plexapp.agents.thetvdb://73800/1/{ep}?lang=en", title, ep))
    backfill_play_guids(conn)
    got = conn.execute("SELECT grandparent_title, grandparent_guid, guid FROM plays ORDER BY id").fetchall()
    assert [tuple(r) for r in got] == [
        ("show a", "plex://show/a", "plex://show/a/e1"),
        ("Show A", "plex://show/a", "plex://show/a/e2"),
        ("Twin", None, "com.plexapp.agents.thetvdb://73800/1/1?lang=en"),   # two shows called that: left alone
    ]
    completion.clear_cache()
    assert completion.for_user(conn, 1, 10)[0]["pct"] == 100
