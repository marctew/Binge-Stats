"""The outside API standard, stage 2 (docs/specs/api-standard.md): feeds in the common item shape,
and the resized-image route."""
import io
import json
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from bingestats.services import api_feeds, api_tokens, settings
from bingestats.web import api_core
from tests.test_api_standard import bearer, healthy, play
from tests.test_pin_lists import build
from tests.test_pins import NOW

LONDON = ZoneInfo("Europe/London")
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


@pytest.fixture
def tok(app_conn):
    return api_tokens.create(app_conn, "Vital Signs desk", "read")


@pytest.fixture
def client(app):
    return app.test_client()


def get(client, tok, url, **headers):
    return client.get(url, headers={**bearer(tok), **headers})


def feed(client, tok, url):
    r = get(client, tok, url)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert set(body) == {"feed", "title", "items", "next_cursor", "generated_at", "refresh_s"}
    for it in body["items"]:
        assert isinstance(it["id"], str) and isinstance(it["title"], str)
        assert None not in it.values()
        if "at" in it:
            assert ISO.match(it["at"]) and it["at_label"]
        if "image" in it:
            assert it["image"].startswith("/api/v1/images/")
    return body


@pytest.fixture
def frozen(monkeypatch):
    """Feeds take 'now' from time.time() in the route; pin fixtures are built around NOW."""
    from bingestats.web import routes_api_std

    class T:
        @staticmethod
        def time():
            return NOW
    monkeypatch.setattr(routes_api_std, "time", T)
    return NOW


def add_item(c, rk, typ, title, added, **kw):
    cols = {"rating_key": rk, "server_id": 1, "type": typ, "title": title, "added_at": added, **kw}
    c.execute(f"INSERT INTO media_items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(cols.values()))


@pytest.fixture
def library(app_conn):
    c = app_conn
    c.execute("INSERT OR IGNORE INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'DS9', 1)")
    add_item(c, 500, "show", "Slow Horses", 1, thumb="/library/metadata/500/thumb/9")
    for i in range(3):    # a season drop over an hour: one arrival
        add_item(c, 501 + i, "episode", f"Ep {i + 1}", NOW - 7200 + i * 1200, grandparent_rating_key=500,
                 grandparent_title="Slow Horses", season_index=5, episode_index=i + 1)
    add_item(c, 510, "episode", "Ep 4", NOW - 3 * 86400, grandparent_rating_key=500, grandparent_title="Slow Horses",
             season_index=5, episode_index=4)          # days apart: its own arrival
    add_item(c, 600, "movie", "Rumours", NOW - 3600, year=1977, thumb="/library/metadata/600/thumb/1")
    add_item(c, 700, "album", "Tusk", NOW - 600, parent_title="Fleetwood Mac")
    add_item(c, 800, "movie", "Gone", NOW - 60, is_present=0)
    c.commit()
    return c


# -- meta and errors ---------------------------------------------------------------------------

def test_meta_lists_the_feeds(client, tok):
    m = get(client, tok, "/api/v1/meta").get_json()
    assert [f["name"] for f in m["feeds"]] == ["now_playing", "arrivals", "history", "alerts", "downloads",
                                               "pins_schedule", "pins_ready"]
    assert all({"name", "title", "refresh_s", "params"} == set(f) for f in m["feeds"])


def test_unknown_feed(client, tok):
    r = get(client, tok, "/api/v1/feeds/arivals")
    assert r.status_code == 404 and r.get_json()["code"] == "not_found"
    assert "There's no feed called 'arivals'" in r.get_json()["error"] and "arrivals" in r.get_json()["error"]


def test_bad_limit_and_since(client, tok):
    assert get(client, tok, "/api/v1/feeds/arrivals?limit=lots").status_code == 400
    r = get(client, tok, "/api/v1/feeds/arrivals?since=yesterday")
    assert r.status_code == 400 and r.get_json()["code"] == "bad_request"


def test_feeds_need_a_token(client):
    assert client.get("/api/v1/feeds/arrivals").status_code == 401


def test_feed_by_url_token_and_cache_headers(client, tok, library):
    r = client.get(f"/api/v1/feeds/arrivals?token={tok}")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "private, max-age=150" and "ETag" in r.headers


# -- arrivals ------------------------------------------------------------------------------------

def test_arrivals_groups_a_season_drop(client, tok, library, frozen):
    items = feed(client, tok, "/api/v1/feeds/arrivals")["items"]
    assert [(i["title"], i.get("subtitle")) for i in items] == [
        ("Tusk", "Fleetwood Mac"), ("Rumours", "1977"),
        ("Slow Horses", "Season 5 · 3 new episodes"), ("Slow Horses", "S05E04 · Ep 4")]
    drop = items[2]
    assert drop["id"] == "plex:501" and drop["image"] == "/api/v1/images/poster/500"
    assert drop["meta"] == {"kind": "show", "rating_key": 501, "show_id": 500, "show_title": "Slow Horses",
                            "season": 5, "episode": 1, "episodes": 3}
    assert drop["url"] == "http://localhost/item/500" and drop["at"] == api_core.iso(NOW - 7200 + 2400)


def test_arrivals_kind_since_and_limit(client, tok, library, frozen):
    shows = feed(client, tok, "/api/v1/feeds/arrivals?kind=show")["items"]
    assert {i["meta"]["kind"] for i in shows} == {"show"}
    since = api_core.iso(NOW - 5000)
    recent = feed(client, tok, f"/api/v1/feeds/arrivals?since={since}")["items"]
    assert [i["title"] for i in recent] == ["Tusk", "Rumours", "Slow Horses"]
    assert len(feed(client, tok, "/api/v1/feeds/arrivals?limit=500")["items"]) == 4      # capped at 100, there are 4


def test_arrivals_cursor_pages_without_gaps_or_repeats(client, tok, library, frozen):
    seen, cursor, pages = [], None, 0
    while True:
        body = feed(client, tok, "/api/v1/feeds/arrivals?limit=1" + (f"&cursor={cursor}" if cursor else ""))
        seen += [i["id"] for i in body["items"]]
        cursor, pages = body["next_cursor"], pages + 1
        if not cursor:
            break
    assert pages == 4 and len(seen) == len(set(seen)) == 4


# -- now playing, history, alerts, downloads ---------------------------------------------------------

def test_now_playing(client, tok, app_conn):
    healthy(app_conn)
    play(app_conn, "sessions_transcode.json")
    [it] = feed(client, tok, "/api/v1/feeds/now_playing")["items"]
    assert it["id"].startswith("session:") and "Marc" in it["subtitle"] and it["tone"] == "warning"
    assert {"user_id", "user_name", "progress_pct", "state", "decision", "bandwidth_kbps", "player"} <= set(it["meta"])


def test_history_pages(client, tok, app_conn):
    c = app_conn
    c.execute("INSERT INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'DS9', 1)")
    c.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (1, 'sarah', 'Sarah', 's')")
    for i in range(5):
        c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, title, "
                  "grandparent_title, season_index, episode_index, decision) VALUES (1, 1, ?, ?, 1800, 'episode', ?, "
                  "'Slow Horses', 5, ?, 'direct_play')", (NOW - 86400 * (i + 1), NOW - 86400 * (i + 1) + 1800, f"Ep {i}", i))
    c.commit()
    first = feed(client, tok, "/api/v1/feeds/history?limit=2")
    assert [i["meta"]["user_name"] for i in first["items"]] == ["Sarah", "Sarah"] and first["next_cursor"]
    second = feed(client, tok, f"/api/v1/feeds/history?limit=2&cursor={first['next_cursor']}")
    ids = [i["id"] for i in first["items"] + second["items"]]
    assert len(set(ids)) == 4
    assert first["items"][0]["subtitle"] == "S05E00 · Ep 0 · Sarah"


def test_alerts_feed(client, tok, app_conn):
    app_conn.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (?, 'stream_limit', NULL, ?)",
                     (NOW, json.dumps({"count": 3, "limit": 2})))
    app_conn.execute("INSERT INTO alerts (created_at, kind, user_id, detail, acknowledged) VALUES (?, 'server_up', NULL, ?, 1)",
                     (NOW - 10, json.dumps({"down_for_s": 120})))
    app_conn.commit()
    [a] = feed(client, tok, "/api/v1/feeds/alerts")["items"]
    assert a["title"] == "Stream limit" and a["tone"] == "danger" and a["meta"]["acknowledged"] is False
    both = feed(client, tok, "/api/v1/feeds/alerts?all=1")["items"]
    assert [i["tone"] for i in both] == ["danger", "success"]


def test_downloads_feed(client, tok, monkeypatch):
    from bingestats.services import arr
    monkeypatch.setattr(arr, "download_queue", lambda conn, now=None: {"ready": True, "items": [
        {"nzo_id": "SAB_1", "title": "Slow Horses", "sub": "S05E03", "pct": 64, "eta_s": 600, "stalled_s": 0,
         "mb": 2048.4, "cat": "tv", "app": "sonarr", "rating_key": 500, "pinned": True, "warning": None},
        {"nzo_id": "SAB_2", "title": "Rumours", "sub": "1977", "pct": 3, "eta_s": None, "stalled_s": 2400,
         "mb": 900, "cat": "movies", "app": "radarr", "rating_key": None, "pinned": False, "warning": None}]})
    items = feed(client, tok, "/api/v1/feeds/downloads")["items"]
    assert [i["id"] for i in items] == ["sab:SAB_1", "sab:SAB_2"]
    assert items[0]["subtitle"] == "S05E03 · 64%" and items[0]["image"] == "/api/v1/images/poster/500"
    assert items[1]["tone"] == "warning" and items[1]["meta"]["stalled"] is True and "image" not in items[1]


def test_downloads_feed_without_sabnzbd(client, tok):
    assert feed(client, tok, "/api/v1/feeds/downloads")["items"] == []


# -- pins -------------------------------------------------------------------------------------------

def test_pins_feeds(client, tok, app_conn, frozen):
    build(app_conn)
    app_conn.commit()
    ready = feed(client, tok, "/api/v1/feeds/pins_ready")["items"]
    assert ready[0]["id"] == "show:111" and ready[0]["title"] == "Pinned Show"
    assert ready[0]["subtitle"] == "S01E02 · Ep 2 · 2 episodes waiting" and ready[0]["meta"]["waiting"] == 2
    sched = feed(client, tok, "/api/v1/feeds/pins_schedule?days=60&back=30")["items"]
    assert sched and all(i["id"].startswith("episode:") for i in sched)
    assert {"tvdb_id", "code", "state", "stalled"} <= set(sched[0]["meta"])
    assert feed(client, tok, "/api/v1/feeds/pins_ready?user_id=424242")["items"] == []


# -- secrets ----------------------------------------------------------------------------------------

def test_no_feed_carries_the_plex_token(client, tok, app_conn, library, frozen):
    healthy(app_conn)
    settings.set(app_conn, "plex.token", "SECRET-PLEX-TOKEN")
    app_conn.commit()
    play(app_conn, "sessions_transcode.json")
    for name in api_feeds.FEEDS:
        data = get(client, tok, f"/api/v1/feeds/{name}").data
        assert b"SECRET-PLEX-TOKEN" not in data and b"X-Plex-Token" not in data, name


# -- at_label ---------------------------------------------------------------------------------------

def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LONDON).timestamp()


@pytest.mark.parametrize("when,label", [
    (ts(2026, 11, 5, 12, 0), "Now"),
    (ts(2026, 11, 5, 12, 40), "in 40 min"),
    (ts(2026, 11, 5, 14, 0), "in 2 h"),
    (ts(2026, 11, 5, 9, 5), "Today 09:05"),
    (ts(2026, 11, 4, 19, 12), "Yesterday 19:12"),
    (ts(2026, 11, 7, 1, 0), "Sat 7 Nov"),
    (ts(2026, 11, 1, 20, 0), "Sun 1 Nov"),
    (ts(2025, 11, 7, 20, 0), "7 Nov 2025"),
])
def test_at_label(when, label):
    assert api_feeds.at_label(when, ts(2026, 11, 5, 12, 0), LONDON) == label


def test_at_label_across_the_clocks_going_back():
    now = ts(2026, 10, 25, 12, 0)                     # BST ended at 02:00 that morning
    assert api_feeds.at_label(ts(2026, 10, 24, 23, 30), now, LONDON) == "Yesterday 23:30"
    assert api_feeds.at_label(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp(), now, LONDON) == \
        "Today 01:30"


# -- images -------------------------------------------------------------------------------------------

def png(w=600, h=900, color=(200, 30, 90)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "PNG")
    return b.getvalue()


class FakePhoto:
    def __init__(self):
        self.calls = []

    def photo(self, path, width, height):
        self.calls.append((path, width, height))

        class R:
            headers = {"Content-Type": "image/jpeg"}
            content = png()
        return R()


@pytest.fixture
def photos(monkeypatch):
    from bingestats.services import activity
    fake = FakePhoto()
    monkeypatch.setattr(activity, "plex_client", lambda conn: fake)
    return fake


def test_poster_webp_by_default_for_browsers(client, tok, library, photos):
    r = get(client, tok, "/api/v1/images/poster/600?w=300", Accept="image/webp,*/*")
    assert r.status_code == 200 and r.mimetype == "image/webp"
    im = Image.open(io.BytesIO(r.data))
    assert im.size == (320, 480)                                        # 300 snaps to 320
    assert r.headers["Cache-Control"] == "public, max-age=86400" and r.headers["ETag"]
    assert photos.calls == [("/library/metadata/600/thumb/1", 320, 480)]


def test_jpeg_when_webp_isnt_accepted(client, tok, library, photos):
    r = get(client, tok, "/api/v1/images/poster/600?w=80", Accept="image/jpeg")
    assert r.mimetype == "image/jpeg" and Image.open(io.BytesIO(r.data)).size == (80, 120)


def test_fmt_quality_and_gray(client, tok, library, photos):
    r = get(client, tok, "/api/v1/images/poster/600?w=160&fmt=jpeg&q=50&gray=1", Accept="image/webp")
    im = Image.open(io.BytesIO(r.data))
    assert r.mimetype == "image/jpeg" and im.mode == "L"


def test_episode_uses_its_shows_poster(client, tok, library, photos):
    assert get(client, tok, "/api/v1/images/poster/502").status_code == 200
    assert photos.calls[-1][0] == "/library/metadata/500/thumb/9"


def test_cache_hit_and_304(client, tok, library, photos):
    a = get(client, tok, "/api/v1/images/poster/600?w=320")
    b = get(client, tok, "/api/v1/images/poster/600?w=320")
    assert len(photos.calls) == 1 and a.headers["ETag"] == b.headers["ETag"] and a.data == b.data
    c = get(client, tok, "/api/v1/images/poster/600?w=320", **{"If-None-Match": a.headers["ETag"]})
    assert c.status_code == 304


def test_image_by_url_token_but_never_a_write_one(client, tok, app_conn, library, photos):
    assert client.get(f"/api/v1/images/poster/600?token={tok}").status_code == 200
    w = api_tokens.create(app_conn, "panel", "write")
    assert client.get(f"/api/v1/images/poster/600?token={w}").status_code == 400


def test_images_dont_count_towards_the_rate_limit(client, tok, library, photos):
    core = client.application.extensions["du_api"]
    core.limiter.limits.per_token_per_min = 2
    for _ in range(5):
        assert get(client, tok, "/api/v1/images/poster/600").status_code == 200
    core.limiter.limits.per_token_per_min = 120


def test_missing_images(client, tok, library, photos):
    assert get(client, tok, "/api/v1/images/poster/424242").status_code == 404
    assert get(client, tok, "/api/v1/images/art/700").status_code == 404          # the album has no art
    r = get(client, tok, "/api/v1/images/banner/600")
    assert r.status_code == 404 and r.get_json()["code"] == "not_found"


def test_show_image_falls_back_to_sonarr(client, tok, app_conn, monkeypatch):
    build(app_conn)
    app_conn.commit()
    from bingestats.services import arr

    class Sonarr:
        url, api_key = "http://sonarr", "KEY"

        class http:
            @staticmethod
            def get(url, headers, timeout):
                assert headers == {"X-Api-Key": "KEY"}

                class R:
                    status_code = 200
                    headers = {"Content-Type": "image/jpeg"}
                    content = png(500, 750)
                return R()
    monkeypatch.setattr(arr, "clients", lambda conn: {"sonarr": Sonarr})
    from bingestats.services import pin_api
    tvdb = next(t for t in (111, 222, 777) if (pin_api.poster_source(app_conn, t) or ("",))[0] == "sonarr")
    r = get(client, tok, f"/api/v1/images/show/{tvdb}?w=160")
    assert r.status_code == 200 and Image.open(io.BytesIO(r.data)).width == 160


def test_prune_image_cache(tmp_path):
    for i in range(5):
        p = tmp_path / f"{i}.webp"
        p.write_bytes(b"x" * 100)
        t = time.time() - 1000 + i
        import os
        os.utime(p, (t, t))
    assert api_core.prune_image_cache(tmp_path, max_bytes=250) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3.webp", "4.webp"]
