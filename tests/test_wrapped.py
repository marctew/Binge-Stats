"""Wrapped (SPEC.md section 13): the numbers, the cache, share links and the public page."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bingestats.services import binges, jobs, settings, wrapped
from bingestats.worker import jobs_runner

LDN = ZoneInfo("Europe/London")
SECRET = "test-secret"


def ts(y, m, d, h=20):
    return int(datetime(y, m, d, h, tzinfo=LDN).timestamp())


def seed(c):
    c.execute("INSERT OR IGNORE INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'Plex', 1)")
    for uid, name in ((1, "Sarah"), (2, "Dad"), (3, "Kids")):
        c.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (?, ?, ?, 'salt0')",
                  (uid, name.lower(), name))
    c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, thumb) "
              "VALUES (10, 1, 'show', 'plex://show/g', 'Ghosts', '/library/metadata/10/thumb/1')")
    for e in (1, 2, 3):
        c.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, grandparent_rating_key, "
                  "season_index, episode_index) VALUES (?, 1, 'episode', ?, ?, 10, 1, ?)", (100 + e, f"plex://ep/{e}", f"E{e}", e))

    def play(uid, start, play_s, media="episode", ep=None, title="X", year=None, decision="direct_play", thumb=None,
             player="Living Room TV"):
        c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, guid, "
                  "grandparent_guid, grandparent_rating_key, grandparent_title, title, year, season_index, episode_index, "
                  "watched, decision, thumb, player_title, product) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, "
                  "'Plex for LG')",
                  (uid, start, start + play_s, play_s, media, f"plex://ep/{ep}" if ep else f"plex://movie/{title}",
                   "plex://show/g" if ep else None, 10 if ep else None, "Ghosts" if ep else None, title, year,
                   1 if ep else None, ep, decision, thumb or ("/library/metadata/10/thumb/1" if ep else
                                                              f"/library/metadata/5{len(title)}/thumb/2"), player))

    # Sarah: Ghosts S1 in one evening of 2026 (finished it), a film, and one play in 2025
    for i, e in enumerate((1, 2, 3)):
        play(1, ts(2026, 3, 14) + i * 1900, 1800, ep=e, title=f"E{e}")
    play(1, ts(2026, 7, 1), 7200, "movie", title="Dune", year=2021, decision="transcode", player="iPhone")
    play(1, ts(2025, 12, 31), 3600, "movie", title="Old Year", year=2020)
    # Dad watches less; Kids a lot more
    play(2, ts(2026, 5, 5), 1800, "movie", title="Short", year=2019)
    play(3, ts(2026, 6, 6), 20 * 3600, "movie", title="Marathon", year=2022)
    binges.rebuild(c)


@pytest.fixture
def data(conn):
    seed(conn)
    return conn


def test_user_year(data):
    w = wrapped.compute(data, 1, 2026)
    assert w["name"] == "Sarah" and w["plays"] == 4 and w["episodes"] == 3 and w["movies"] == 1
    assert w["watch_s"] == 3 * 1800 + 7200 and w["hours"] == 3.5
    assert w["titles"] == 2 and w["days_watched"] == 2
    assert w["top_shows"][0]["title"] == "Ghosts" and w["top_shows"][0]["hours"] == 1.5
    assert w["top_movies"][0]["title"] == "Dune"
    assert w["binge"]["episodes"] == 3 and w["binge"]["show"] == "Ghosts"
    assert w["busiest_day"] == {"date": "2026-07-01", "hours": 2.0}
    assert w["months"][2] == 1.5 and w["months"][6] == 2.0 and sum(w["months"]) == 3.5
    assert w["device"] == {"name": "iPhone", "app": "Plex for LG"}            # most watch time: 2 h vs 1.5 h
    assert w["direct_pct"] == 75
    assert w["first"]["title"] == "Ghosts" and w["first"]["sub"] == "S01E01 · E1" and w["last"]["title"] == "Dune"
    assert [s["title"] for s in w["finished"]] == ["Ghosts"]
    assert w["rank"] == {"position": 2, "of": 3, "top_pct": 67}
    assert w["comparison"] is not None and "football" in w["comparison"]
    assert wrapped.compute(data, 1, 2025)["finished"] == []                   # finished in 2026, not 2025


def test_server_year_and_years(data):
    w = wrapped.compute(data, wrapped.SERVER, 2026)
    assert w["name"] == "Everyone" and w["plays"] == 6 and w["rank"] is None
    assert [p["name"] for p in w["people"]] == ["Kids", "Sarah", "Dad"]
    assert w["comparison"].startswith("that's 1.1 trips London")
    assert wrapped.years(data) == [2026, 2025] and wrapped.years(data, 2) == [2026]


def test_empty_year(data):
    w = wrapped.compute(data, 2, 2024)
    assert w["plays"] == 0 and w["first"] is None and w["busiest_day"] is None and w["months"] == [0.0] * 12


def test_comparisons():
    assert wrapped.comparison(0.5) is None
    assert wrapped.comparison(3) == "that's 2 football matches"
    assert wrapped.comparison(22) == "that's 1.0 trip London → Sydney"
    assert wrapped.comparison(500) == "that's 23 trips London → Sydney"


def test_cache_and_refresh_job(data):
    now = ts(2026, 10, 31)
    assert wrapped.cached(data, 1, 2026, now) is None
    res = wrapped.refresh(data, 2026, now=now)
    assert res == {"year": 2026, "computed": 4}                               # server + 3 people
    assert wrapped.cached(data, 1, 2026, now + 3600)["hours"] == 3.5
    assert wrapped.cached(data, 1, 2026, now + wrapped.FRESH_S + 1) is None  # running year goes stale daily
    wrapped.refresh(data, 2025, now=now)
    assert wrapped.cached(data, 1, 2025, now + 400 * 86400) is not None      # a past year doesn't
    data.execute("UPDATE wrapped_cache SET data = json_set(data, '$.version', 0)")
    assert wrapped.cached(data, 1, 2025, now) is None                        # old shape ignored


def test_wrapped_job_scheduled_daily_and_run(data, tmp_path):
    settings.set(data, "plex.url", "http://p")
    settings.set(data, "plex.token", "t")
    for k in ("library.last_sync_at", "users.last_sync_at", "geo.last_run_at", "backup.last_run_at"):
        settings.set(data, k, 10**10)
    settings.set(data, "geo.enabled", False)
    [jid] = jobs_runner.schedule_due(data, now=10**10)
    job = jobs.claim_next(data)
    assert job["kind"] == "wrapped"

    class Cfg:
        data_dir = tmp_path
    result = jobs_runner.run_job(data, Cfg, job)
    assert result["computed"] >= 1 and settings.get(data, "wrapped.last_run_at") > 0
    assert jobs_runner.schedule_due(data, now=settings.get(data, "wrapped.last_run_at") + 3600) == []


def test_tokens_and_revoke(data):
    t = wrapped.share_token(data, SECRET, 1, 2026)
    assert wrapped.read_token(data, SECRET, t) == (1, 2026)
    assert wrapped.read_token(data, "other-secret", t) is None
    assert wrapped.read_token(data, SECRET, t[:-2] + "xx") is None
    assert wrapped.read_token(data, SECRET, "garbage") is None
    # another user's salt is the same here, but the payload is signed: can't swap the id
    forged = t.replace(t.split(".")[0], wrapped.share_token(data, SECRET, 2, 2026).split(".")[0])
    assert wrapped.read_token(data, SECRET, forged) is None
    wrapped.revoke(data, 1)
    assert wrapped.read_token(data, SECRET, t) is None
    t2 = wrapped.share_token(data, SECRET, 1, 2026)
    assert t2 != t and wrapped.read_token(data, SECRET, t2) == (1, 2026)
    s = wrapped.share_token(data, SECRET, wrapped.SERVER, 2026)
    assert wrapped.read_token(data, SECRET, s) == (0, 2026)
    wrapped.revoke(data, wrapped.SERVER)
    assert wrapped.read_token(data, SECRET, s) is None


def test_image_paths(data):
    paths = wrapped.image_paths(wrapped.compute(data, 1, 2026))
    assert "/library/metadata/10/thumb/1" in paths and all(p.startswith("/library/") for p in paths)


# -- pages --------------------------------------------------------------------------------

@pytest.fixture
def site(app, app_conn, logged_in):
    seed(app_conn)
    return logged_in


def test_admin_pages(site, app_conn):
    body = site.get("/wrapped?year=2026").data.decode()
    assert "The whole server" in body and "Sarah" in body and "Kids" in body
    body = site.get("/wrapped/2026/1").data.decode()
    assert "Sarah&#39;s year on Plex" in body and "Copy share link" in body and "/wrapped/s/" in body
    assert "Ghosts" in body and "episodes of Ghosts in one go" in body and "#2" in body
    assert "Who watched most" in site.get("/wrapped/2026/0").data.decode()
    assert site.get("/wrapped/2026/99").status_code == 404
    assert "Nothing to wrap yet" in site.get("/wrapped/2024/1").data.decode()
    assert "2026 Wrapped" in site.get("/users/1").data.decode()


def _share_url(site, uid=1):
    body = site.get(f"/wrapped/2026/{uid}").data.decode()
    import re
    return re.search(r'value="http://localhost(/wrapped/s/[^"]+)"', body).group(1)


def test_public_link_needs_no_login_and_revoke_kills_it(site, app):
    url = _share_url(site)
    anon = app.test_client()
    r = anon.get(url)
    body = r.data.decode()
    assert r.status_code == 200 and "Sarah&#39;s year on Plex" in body
    assert "Copy share link" not in body and "Sign out" not in body and "csrf" not in body.lower()
    assert r.headers["X-Robots-Tag"] == "noindex"
    # images: only this page's artwork
    assert f"{url}/img?path=/library/metadata/10/thumb/1" in body
    assert anon.get(f"{url}/img?path=/library/metadata/999/thumb/1").status_code == 404
    assert anon.get("/img/plex?path=/library/metadata/10/thumb/1").status_code == 302   # the normal proxy needs login
    assert anon.get("/wrapped/2026/1").status_code == 302
    # revoke
    r = site.post("/wrapped/1/revoke", {"year": "2026"})
    assert r.status_code == 302
    assert anon.get(url).status_code == 404 and anon.get(f"{url}/img?path=/library/metadata/10/thumb/1").status_code == 404
    assert _share_url(site) != url and anon.get(_share_url(site)).status_code == 200


def test_public_image_served_from_plex(site, app, monkeypatch):
    from bingestats.plex import client as client_mod

    class R:
        headers = {"Content-Type": "image/jpeg"}
        content = b"\xff\xd8jpeg"
    monkeypatch.setattr(client_mod.PlexClient, "photo", lambda self, path, w, h: R())
    url = _share_url(site)
    r = app.test_client().get(f"{url}/img?path=/library/metadata/10/thumb/1&w=300")
    assert r.status_code == 200 and r.data == b"\xff\xd8jpeg"


def test_share_link_uses_public_url(site, app_conn):
    settings.set(app_conn, "ui.public_url", "https://binge.example.co.uk")
    body = site.get("/wrapped/2026/1").data.decode()
    assert 'value="https://binge.example.co.uk/wrapped/s/' in body
