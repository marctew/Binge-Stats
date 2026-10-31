"""Transcode insights and library health (SPEC.md section 12), plus the Insights pages and API."""
import csv
import io
import json

import pytest

from bingestats.services import api_tokens, binges, insights, stats

NOW = 1_800_000_000
DAY = 86400


def item(c, rk, typ, title, *, guid=None, library=4, added=NOW - 400 * DAY, size=None, parent=None, season=1,
         ep=None, vcodec=None, acodec=None, present=1):
    c.execute("INSERT INTO media_items (rating_key, server_id, library_id, type, guid, title, added_at, size_bytes, "
              "grandparent_rating_key, season_index, episode_index, video_codec, audio_codec, is_present) "
              "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (rk, library, typ, guid or f"plex://{typ}/{rk}", title, added, size, parent, season, ep, vcodec, acodec,
               present))


def play(c, user, media, guid, start, *, decision="direct_play", reasons=(), play_s=3600, gp_guid=None, gp_rk=None,
         title="X", group_id=None, src_vc="h264", src_ac="aac", product="Plex Web"):
    c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, guid, grandparent_guid, "
              "grandparent_rating_key, grandparent_title, title, decision, transcode_reasons, group_id, src_video_codec, "
              "src_audio_codec, product, player_title, watched) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'TV', 1)",
              (user, start, start + play_s, play_s, media, guid, gp_guid, gp_rk, "Show" if gp_guid or gp_rk else None,
               title, decision, json.dumps(list(reasons)), group_id, src_vc, src_ac, product))


@pytest.fixture
def lib(conn):
    c = conn
    for uid, name in ((1, "sarah"), (2, "dad")):
        c.execute("INSERT INTO users (id, username, share_salt) VALUES (?, ?, 's')", (uid, name))
    c.execute("INSERT OR IGNORE INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'Plex', 1)")
    c.execute("INSERT INTO libraries (id, server_id, name, type) VALUES (4, 1, 'Films', 'movie'), (5, 1, 'TV', 'show')")
    item(c, 100, "movie", "Big Unwatched", size=60e9)                           # never played, old: stale + never
    item(c, 101, "movie", "Old Favourite", size=20e9)                           # played 2 years ago: stale
    item(c, 102, "movie", "Recent Hit", size=10e9)                              # played last week
    item(c, 103, "movie", "Just Added", size=5e9, added=NOW - 10 * DAY)         # new, unwatched: neither
    item(c, 104, "movie", "Gone", size=1e9, present=0)                          # not in library: ignored
    item(c, 200, "show", "Old Show", library=5)                                 # episodes added long ago
    for e in (1, 2):
        item(c, 2000 + e, "episode", f"E{e}", library=5, size=2e9, parent=200, ep=e)
    item(c, 300, "show", "Fresh Show", library=5)
    item(c, 3001, "episode", "E1", library=5, size=3e9, parent=300, ep=1, added=NOW - 5 * DAY)
    play(c, 1, "movie", "plex://movie/101", NOW - 730 * DAY)
    play(c, 2, "movie", "plex://movie/102", NOW - 7 * DAY, play_s=7200)
    play(c, 1, "episode", "plex://episode/2001", NOW - 500 * DAY, gp_rk=200)   # no grandparent_guid: rk fallback
    return c


# -- library health ----------------------------------------------------------------------

def test_library_health_lists(lib):
    d = insights.library_health(lib, now=NOW)
    assert [r["title"] for r in d["stale"]] == ["Big Unwatched", "Old Favourite", "Old Show"]
    assert [r["title"] for r in d["never"]] == ["Big Unwatched"]
    old = next(r for r in d["stale"] if r["title"] == "Old Show")
    assert old["size_bytes"] == 4e9 and old["episodes"] == 2 and old["plays"] == 1 and old["last_user"] == "sarah"
    fav = next(r for r in d["stale"] if r["title"] == "Old Favourite")
    assert fav["last_played"] == NOW - 730 * DAY
    t = d["totals"]
    assert t["stale_items"] == 3 and t["stale_bytes"] == 84e9 and t["item_count"] == 6
    # cost: never-watched (∞) first by size, then worst GB/hour
    assert [r["title"] for r in d["cost"]][:3] == ["Big Unwatched", "Just Added", "Fresh Show"]
    hit = next(r for r in d["cost"] if r["title"] == "Recent Hit")
    assert hit["gb_per_hour"] == 5.0 and hit["hours"] == 2.0


def test_library_health_filters_and_stale_setting(lib):
    from bingestats.services import settings
    assert [r["title"] for r in insights.library_health(lib, kind="show", now=NOW)["stale"]] == ["Old Show"]
    assert insights.library_health(lib, library_id=4, now=NOW)["totals"]["item_count"] == 4
    settings.set(lib, "library.stale_months", 14)        # everything was added ~13 months ago
    assert insights.library_health(lib, now=NOW)["stale"] == []


def test_health_csv(lib):
    d = insights.library_health(lib, now=NOW)
    rows = list(csv.DictReader(io.StringIO(insights.health_csv(d["stale"], stats.ZoneInfo("Europe/London")))))
    assert rows[0]["title"] == "Big Unwatched" and rows[0]["size_gb"] == "60.0" and rows[0]["last_played"] == ""
    assert rows[2]["last_played_by"] == "sarah"


# -- transcodes ---------------------------------------------------------------------------

@pytest.fixture
def tx(lib):
    c = lib
    item(c, 400, "movie", "HEVC Film", size=30e9, vcodec="hevc", acodec="truehd")
    for i in range(4):          # 3 of 4 transcoded for the file
        file = i < 3
        play(c, 1 + i % 2, "movie", "plex://movie/400", NOW - (i + 1) * DAY, title="HEVC Film",
             decision="transcode" if file else "direct_play", reasons=("video_codec", "audio_codec") if file else (),
             src_vc="hevc", src_ac="truehd", product="Plex for LG")
    for i in range(4):          # transcoded, but for bandwidth: not a candidate
        play(c, 2, "movie", "plex://movie/102", NOW - (i + 1) * DAY, decision="transcode", reasons=("video_bitrate",))
    for i in range(2):          # only 2 plays: below the minimum
        play(c, 1, "movie", "plex://movie/101", NOW - (i + 1) * DAY, decision="transcode", reasons=("audio_codec",))
    return c


def rng90(c):
    return stats.make_range(c, preset="90d", now=NOW)


def test_reencode_candidates(tx):
    cand, second = insights.reencode_candidates(tx)
    assert second["title"] == "X" and (second["plays"], second["file_transcodes"]) == (3, 2)   # 2 of 3 is enough
    assert cand["title"] == "HEVC Film" and cand["rating_key"] == 400
    assert (cand["plays"], cand["file_transcodes"], cand["pct"], cand["people"]) == (4, 3, 75, 2)
    assert cand["video_codec"] == "hevc" and cand["audio_codec"] == "truehd" and cand["size_bytes"] == 30e9
    assert cand["reasons"] == ["Video codec not supported", "Audio codec not supported"]


def test_transcode_breakdowns(tx):
    r = rng90(tx)
    s = insights.transcode_summary(tx, r)
    assert (s["plays"], s["transcodes"], s["file_transcodes"]) == (11, 9, 5)
    reasons = {x["name"]: x["value"] for x in insights.reasons(tx, r)["items"]}
    assert reasons["Bitrate reduced (quality setting)"] == 4 and reasons["Audio codec not supported"] == 5
    by_user = {x["name"]: x for x in insights.transcode_by(tx, r, "user")}
    assert by_user["dad"]["transcodes"] == 5 and by_user["dad"]["plays"] == 7 and by_user["dad"]["pct"] == 71
    apps = {x["name"]: x["pct"] for x in insights.transcode_by(tx, r, "product")}
    assert apps == {"Plex Web": 86, "Plex for LG": 75}
    codecs = [x["name"] for x in insights.transcode_by(tx, r, "src_video_codec")]
    assert codecs == ["H264", "HEVC"]


def test_decision_share_adds_to_100(tx):
    d = insights.decision_share(tx, stats.make_range(tx, preset="7d", now=NOW))
    assert [s["name"] for s in d["series"]] == ["Direct Play", "Transcode"]
    for i, n in enumerate(d["counts"]):
        if n:
            assert sum(s["data"][i] for s in d["series"]) == pytest.approx(100, abs=0.2)


# -- pages and API ------------------------------------------------------------------------

@pytest.fixture
def web(app_conn, logged_in):
    c = app_conn
    for uid, name in ((1, "sarah"), (2, "dad")):
        c.execute("INSERT INTO users (id, username, share_salt) VALUES (?, ?, 's')", (uid, name))
    c.execute("INSERT OR IGNORE INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'Plex', 1)")
    c.execute("INSERT INTO libraries (id, server_id, name, type) VALUES (4, 1, 'Films', 'movie'), (5, 1, 'TV', 'show')")
    item(c, 100, "movie", "Big Unwatched", size=60e9, added=1_000_000)
    item(c, 200, "show", "Ghosts", library=5)
    for e in range(1, 5):
        item(c, 2000 + e, "episode", f"E{e}", library=5, size=1e9, parent=200, ep=e)
    import time
    t = int(time.time()) - 10 * DAY
    for e in range(1, 5):
        play(c, 1, "episode", f"plex://episode/{2000 + e}", t + e * 2000, play_s=1500, gp_guid="plex://show/200",
             title=f"E{e}", decision="transcode" if e == 1 else "direct_play", reasons=("audio_codec",) if e == 1 else ())
    binges.rebuild(c)
    return logged_in


def test_insights_pages(web):
    r = web.get("/insights/binges")
    body = r.data.decode()
    assert "Longest binges" in body and "/item/200" in body and "4 <span" in body
    body = web.get("/insights/transcodes").data.decode()
    assert "Re-encode candidates" in body and "Why things transcoded" in body
    body = web.get("/insights/library-health").data.decode()
    assert "in stale content across" in body and "Big Unwatched" in body
    assert "Big Unwatched" in web.get("/insights/library-health?tab=never&type=movie").data.decode()
    assert "Big Unwatched" not in web.get("/insights/library-health?type=show").data.decode()
    r = web.get("/insights/library-health.csv?tab=stale")
    assert r.mimetype == "text/csv" and "Big Unwatched" in r.data.decode()
    assert web.get("/insights").status_code == 302


def test_user_page_shows_binges_and_completion(web):
    body = web.get("/users/1").data.decode()
    assert "Longest binges" in body and "Watching" in body and "finished" in body


def test_insights_api(web, app, app_conn):
    h = {"Authorization": f"Bearer {api_tokens.create(app_conn, 'ha', 'read')}"}
    c = app.test_client()
    d = c.get("/api/v1/insights/binges?range=30d", headers=h).get_json()
    assert d["totals"]["binges"] == 1 and d["longest"][0]["episode_count"] == 4
    d = c.get("/api/v1/insights/transcodes?range=30d", headers=h).get_json()
    assert d["summary"]["transcodes"] == 1 and d["by"]["user"][0]["name"] == "sarah"
    d = c.get("/api/v1/insights/transcodes/share?range=30d", headers=h).get_json()
    assert {s["name"] for s in d["series"]} == {"Direct Play", "Transcode"}
    assert c.get("/api/v1/insights/transcodes/reasons?range=30d", headers=h).get_json()["items"][0]["value"] == 1
    d = c.get("/api/v1/insights/library-health?type=movie", headers=h).get_json()
    assert d["stale"][0]["title"] == "Big Unwatched" and d["totals"]["item_count"] == 1
    d = c.get("/api/v1/users/1/completion", headers=h).get_json()
    assert d["finished"][0]["title"] == "Ghosts" and d["finished"][0]["pct"] == 100
    assert c.get("/api/v1/users/99/completion", headers=h).status_code == 404
