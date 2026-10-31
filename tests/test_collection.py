"""Collection (make-up, re-encode, upgrades, gaps) and Playback (buffering, slow transcodes)."""
import json

import pytest

from bingestats.services import api_tokens, collection, playback, stats

NOW = 1_800_000_000
GB = 10**9


def item(c, rk, typ, title, *, codec="h264", res="1080", audio="eac3", size=GB, library=4, parent=None, season=None,
         ep=None, present=1, guid=None):
    c.execute("INSERT INTO media_items (rating_key, server_id, library_id, type, guid, title, video_codec, video_resolution, "
              "audio_codec, size_bytes, grandparent_rating_key, grandparent_title, season_index, episode_index, is_present) "
              "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (rk, library, typ, guid or f"plex://{typ}/{rk}", title, codec, res, audio, size, parent,
               "Holey Show" if parent == 500 else ("Fine Show" if parent == 600 else None), season, ep, present))


@pytest.fixture
def lib(conn):
    c = conn
    c.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    c.execute("INSERT INTO libraries (id, server_id, name, type) VALUES (4, 1, 'Films', 'movie'), (5, 1, 'TV', 'show')")
    item(c, 1, "movie", "Big H264", size=40 * GB)
    item(c, 2, "movie", "Already HEVC", codec="hevc", size=10 * GB)
    item(c, 3, "movie", "Old DVD", codec="mpeg2video", res="sd", size=4 * GB)
    item(c, 4, "movie", "Gone", size=99 * GB, present=0)
    item(c, 500, "show", "Holey Show", library=5, size=None, codec=None, res=None, audio=None)
    for s, eps in ((1, (1, 2, 3)), (2, (1, 2, 5, 6, 9)), (4, (1, 2))):        # S02 has holes; S03 missing
        for e in eps:
            item(c, 5000 + s * 100 + e, "episode", f"E{e}", library=5, parent=500, season=s, ep=e,
                 res="720" if s == 1 else "1080", size=2 * GB)
    item(c, 600, "show", "Fine Show", library=5, size=None, codec=None, res=None, audio=None)
    for e in (1, 2, 3):
        item(c, 6000 + e, "episode", f"E{e}", library=5, parent=600, season=1, ep=e, codec="av1")
    item(c, 6100, "episode", "Daily", library=5, parent=600, season=2026, ep=20260101)   # date-numbered: ignored
    item(c, 6101, "episode", "Special", library=5, parent=600, season=0, ep=7)          # specials: ignored
    c.execute("INSERT INTO users (id, username, share_salt) VALUES (1, 'u', 's')")
    c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, guid) "
              "VALUES (1, 1, 1, 2, 1, 'movie', 'plex://movie/3'), (1, 1, 3, 4, 1, 'movie', 'plex://movie/3')")
    return c


def test_makeup(lib):
    m = collection.makeup(lib)
    assert m["files"] == 3 + 10 + 5      # films present + Holey episodes + Fine (3, a daily, a special)
    codecs = {c["key"]: c for c in m["codecs"]}
    assert codecs["h264"]["files"] == 13 and codecs["av1"]["files"] == 3
    assert m["old_codec_bytes"] == 40 * GB + 4 * GB + 20 * GB + 2 * GB      # h264 film, mpeg2, Holey eps, daily + special
    assert m["saving_bytes"] == int(40 * GB * 0.5 + 4 * GB * 0.75 + 20 * GB * 0.5 + 2 * GB * 0.5)
    assert [r["key"] for r in m["resolutions"]][:3] == ["1080", "720", "sd"]
    assert m["low_res_items"] == 3 + 1                                      # S01 x3 at 720p, the DVD
    assert collection.makeup(lib, kind="movie")["files"] == 3
    assert collection.makeup(lib, library_id=5)["files"] == 15


def test_reencode_rolls_shows_up_and_ranks_by_saving(lib):
    r = collection.reencode(lib)
    assert [x["title"] for x in r] == ["Big H264", "Holey Show", "Old DVD", "Fine Show"]
    show = r[1]
    assert show["type"] == "show" and show["files"] == 10 and show["saving"] == int(10 * GB) and "720" in show["resolution"]


def test_upgrades_most_played_first(lib):
    u = collection.upgrades(lib)
    assert u[0]["title"] == "Old DVD" and u[0]["plays"] == 2 and u[0]["resolution"] == "sd"
    assert u[1]["type"] == "season" and u[1]["season"] == 1 and u[1]["files"] == 3


def test_gaps(lib):
    [g] = collection.gaps(lib)
    assert g["title"] == "Holey Show" and g["missing_seasons"] == [3]
    [hole] = g["holes"]
    assert hole["season"] == 2 and hole["missing"] == [3, 4, 7, 8] and hole["text"] == "E03–E04, E07–E08"
    assert g["missing_episodes"] == 4
    csv = collection.to_csv("gaps", [g])
    assert "Holey Show" in csv and "S02: E03–E04, E07–E08" in csv


def test_csvs(lib):
    assert "estimated_saving_gb" in collection.to_csv("reencode", collection.reencode(lib))
    assert "Old DVD" in collection.to_csv("upgrades", collection.upgrades(lib))


# -- playback -----------------------------------------------------------------------------

@pytest.fixture
def plays(conn):
    c = conn
    for uid, name in ((1, "Alex"), (2, "Sam")):
        c.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (?, ?, ?, 's')", (uid, name.lower(), name))
    c.execute("INSERT INTO ip_geo (ip, is_private, city, country, looked_up_at) VALUES ('81.2.69.160', 0, 'Leeds', 'United Kingdom', 1)")

    def play(uid, start, play_s, buf, *, source="live", local=1, ip="192.168.1.5", reasons=(), decision="direct_play",
             player="TV"):
        c.execute("INSERT INTO plays (server_id, source, user_id, started_at, stopped_at, play_s, media_type, title, "
                  "buffer_count, is_local, ip_address, transcode_reasons, decision, player_title) "
                  "VALUES (1, ?, ?, ?, ?, ?, 'movie', 'Film', ?, ?, ?, ?, ?, ?)",
                  (source, uid, start, start + play_s, play_s, buf, local, ip, json.dumps(list(reasons)), decision, player))
    play(1, NOW - 3600, 7200, 0)
    play(2, NOW - 7200, 3600, 3, local=0, ip="81.2.69.160", decision="transcode", player="Phone")
    play(2, NOW - 9000, 1800, 1, local=0, ip="81.2.69.160", reasons=("transcoder_slow",), decision="transcode", player="Phone")
    play(1, NOW - 9500, 3600, 9, source="tautulli")                       # imported: never counted
    return c


def test_playback(plays):
    rng = stats.make_range(plays, preset="7d", now=NOW)
    s = playback.summary(plays, rng)
    assert (s["plays"], s["buffered"], s["events"], s["slow"]) == (3, 2, 4, 1)
    assert s["per_10h"] == round(4 / (12600 / 36000), 1)
    who = playback.breakdown(plays, rng, "user")
    assert [w["name"] for w in who] == ["Sam"] and who[0]["events"] == 4
    assert playback.breakdown(plays, rng, "place")[0]["name"] == "Leeds, United Kingdom"
    assert playback.breakdown(plays, rng, "decision")[0]["name"] == "Transcode"
    w = playback.worst(plays, rng)
    assert [x["buffer_count"] for x in w] == [3, 1] and w[1]["slow"] and w[0]["place"] == "Leeds, United Kingdom"
    with pytest.raises(ValueError):
        playback.breakdown(plays, rng, "nope")


# -- pages and API ------------------------------------------------------------------------

def test_pages_and_api(logged_in, app, app_conn):
    c = app_conn
    c.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    c.execute("INSERT INTO libraries (id, server_id, name, type) VALUES (5, 1, 'TV', 'show')")
    item(c, 500, "show", "Holey Show", library=5, size=None)
    for e in (1, 3):
        item(c, 5000 + e, "episode", f"E{e}", library=5, parent=500, season=1, ep=e)
    body = logged_in.get("/insights/collection").data.decode()
    assert "Re-encode for space" in body and "Holey Show" in body and "Gaps in shows · 1" in body
    body = logged_in.get("/insights/collection?list=gaps").data.decode()
    assert "missing E02" in body
    assert "Gaps are about shows" in logged_in.get("/insights/collection?list=gaps&type=movie").data.decode()
    r = logged_in.get("/insights/collection.csv?list=upgrades")
    assert r.mimetype == "text/csv"
    body = logged_in.get("/insights/playback").data.decode()
    assert "no live plays yet" in body and "Tautulli" in body
    h = {"Authorization": f"Bearer {api_tokens.create(app_conn, 'x', 'read')}"}
    d = app.test_client().get("/api/v1/insights/collection?type=show", headers=h).get_json()
    assert d["makeup"]["files"] == 2 and d["gaps"][0]["holes"][0]["missing"] == [2]
    d = app.test_client().get("/api/v1/insights/playback?range=30d", headers=h).get_json()
    assert d["summary"]["plays"] == 0


def test_show_titles_come_from_the_show(lib):
    lib.execute("UPDATE media_items SET grandparent_title = NULL WHERE type = 'episode'")
    assert "Holey Show" in [x["title"] for x in collection.reencode(lib)]
    assert any(x["title"] == "Holey Show" for x in collection.upgrades(lib) if x["type"] == "season")


def test_gaps_nearly_complete_first(lib):
    # a show you dip into: one episode each of S01 and S05
    item(lib, 700, "show", "Dip In", library=5, size=None)
    for s in (1, 5):
        item(lib, 7000 + s, "episode", "E", library=5, parent=700, season=s, ep=10)
    order = [g["title"] for g in collection.gaps(lib)]
    assert order == ["Holey Show", "Dip In"]
    assert collection.gaps(lib)[0]["complete_pct"] > collection.gaps(lib)[1]["complete_pct"]
