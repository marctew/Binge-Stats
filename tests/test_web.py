import responses

from conftest import load_fixture

from bingestats.services import auth, settings
from bingestats.worker.poller import Poller


def test_first_run_redirects_to_setup(browser):
    r = browser.get("/activity")
    assert r.status_code == 302 and r.headers["Location"].endswith("/setup")


def test_setup_creates_admin_then_asks_for_plex(browser, app_conn):
    browser.get("/setup")
    r = browser.post("/setup", {"username": "marc", "password": "longenough", "password2": "longenough"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/setup/plex")
    assert auth.admin_exists(app_conn)
    r = browser.get("/activity")
    assert r.status_code == 302 and r.headers["Location"].endswith("/setup/plex")
    assert browser.get("/setup/plex").status_code == 200


def test_setup_rejects_short_or_mismatched_password(browser, app_conn):
    browser.get("/setup")
    r = browser.post("/setup", {"username": "marc", "password": "short", "password2": "short"})
    assert b"at least 8" in r.data
    r = browser.post("/setup", {"username": "marc", "password": "longenough", "password2": "different1"})
    assert b"don&#39;t match" in r.data
    assert not auth.admin_exists(app_conn)


def test_post_without_csrf_is_rejected(app, app_conn):
    auth.set_admin(app_conn, "marc", "correct horse")
    c = app.test_client()
    r = c.post("/login", data={"username": "marc", "password": "correct horse"})
    assert r.status_code == 400


def test_login_wrong_password(browser, app_conn):
    auth.set_admin(app_conn, "marc", "correct horse")
    browser.get("/login")
    r = browser.post("/login", {"username": "marc", "password": "nope"})
    assert r.status_code == 200 and b"Wrong username or password" in r.data


def test_login_next_is_local_only(browser, app_conn):
    auth.set_admin(app_conn, "marc", "correct horse")
    browser.get("/login")
    r = browser.post("/login?next=//evil.example", {"username": "marc", "password": "correct horse"})
    assert r.headers["Location"] == "/"


def test_htmx_request_when_logged_out_gets_hx_redirect(browser, app_conn):
    auth.set_admin(app_conn, "marc", "correct horse")
    r = browser.client.get("/activity/partial", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"].endswith("/login")


def test_pages_render_empty(logged_in):
    for url in ("/", "/dashboard/now", "/graphs", "/activity", "/activity/partial", "/history", "/settings", "/setup/plex"):
        r = logged_in.get(url)
        assert r.status_code == 200, url
    assert b"Nothing playing right now" in logged_in.get("/activity").data
    assert b"No history yet" in logged_in.get("/history").data


def _seed(app_conn, sessions=("sessions_direct_play.json", "sessions_transcode.json"), finish=True):
    from conftest import Clock, FakePlex
    plex, clock = FakePlex(), Clock()
    p = Poller(app_conn, plex, clock=clock)
    plex.play(*sessions)
    p.poll_once()
    for _ in range(4):
        clock.advance(60)
        p.poll_once()
    if finish:
        plex.stop_all()
        for _ in range(3):
            clock.advance(5)
            p.poll_once()
    return plex


def test_activity_shows_sessions(logged_in, app_conn):
    _seed(app_conn, finish=False)
    r = logged_in.get("/activity")
    body = r.data.decode()
    assert "The Jury: Murder Trial" in body and "Dune: Part Two" in body
    assert "Transcode" in body and "Subtitles burned in" in body
    assert "plex.tv" not in body and "tok" not in body.split("csrf")[0]  # no token or avatar URLs leak
    assert "/img/plex?path=" in body


def test_history_lists_filters_and_exports(logged_in, app_conn):
    _seed(app_conn)
    body = logged_in.get("/history").data.decode()
    assert "Dune: Part Two" in body and "The Jury: Murder Trial" in body
    body = logged_in.get("/history?type=movie").data.decode()
    assert "Dune: Part Two" in body and "The Jury" not in body
    body = logged_in.get("/history?decision=direct_play").data.decode()
    assert "Dune" not in body
    body = logged_in.get("/history?q=jury").data.decode()
    assert "The Jury" in body and "Dune" not in body
    csv_body = logged_in.get("/history.csv").data.decode()
    assert csv_body.startswith("started_at,") and csv_body.count("\n") == 3


def test_history_paging(logged_in, app_conn):
    for i in range(55):
        app_conn.execute(
            "INSERT OR IGNORE INTO users (id, username, share_salt) VALUES (1, 'u', 's')")
        app_conn.execute(
            "INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, title) "
            "VALUES (1, 1, ?, ?, 100, 'movie', ?)", (1_700_000_000 + i * 1000, 1_700_000_100 + i * 1000, f"Film {i:02d}"))
    body = logged_in.get("/history").data.decode()
    assert "Film 54" in body and "Film 05" in body and "Film 04" not in body
    assert "Load more" in body
    import re
    cursor = re.search(r"before=([0-9]+(?:%3A|:)[0-9]+)", body).group(1)
    more = logged_in.client.get(f"/history?before={cursor}", headers={"HX-Request": "true"}).data.decode()
    assert "Film 04" in more and "Film 00" in more and "Load more" not in more


def test_history_parts(logged_in, app_conn):
    app_conn.execute("INSERT INTO users (id, username, share_salt) VALUES (1, 'u', 's')")
    app_conn.execute("INSERT INTO plays (id, server_id, user_id, started_at, stopped_at, play_s, media_type, title) "
                     "VALUES (1, 1, 1, 100, 200, 100, 'movie', 'Film')")
    app_conn.execute("INSERT INTO plays (id, server_id, group_id, user_id, started_at, stopped_at, play_s, media_type, title) "
                     "VALUES (2, 1, 1, 1, 300, 400, 100, 'movie', 'Film')")
    body = logged_in.get("/history").data.decode()
    assert "2 parts" in body and body.count(">Film<") == 1
    assert logged_in.get("/history/1/parts").data.decode().count("Part ") == 2


def test_stop_stream(logged_in, app_conn, monkeypatch):
    _seed(app_conn, finish=False)
    calls = []
    from bingestats.plex import client as client_mod
    monkeypatch.setattr(client_mod.PlexClient, "terminate", lambda self, sid, reason: calls.append((sid, reason)))
    r = logged_in.post("/activity/transcodesession0001/stop", {"message": "Bedtime"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and b"Stream stopped" in r.data
    assert calls == [("transcodesession0001", "Bedtime")]
    r = logged_in.post("/activity/nope/stop", {"message": ""}, headers={"HX-Request": "true"})
    assert b"already ended" in r.data


def test_settings_save_and_validate(logged_in, app_conn):
    logged_in.get("/settings")
    form = {"ui.timezone": "Europe/London", "poll.interval_s": "10", "poll.use_websocket": "on",
            "ingest.min_play_s": "45", "ingest.grace_polls": "3", "ingest.watched_pct.movie": "90",
            "ingest.watched_pct.episode": "85", "ingest.watched_pct.track": "50", "ingest.group_resume_h": "24"}
    logged_in.post("/settings", form)
    assert settings.get(app_conn, "poll.interval_s") == 10
    assert settings.get(app_conn, "ingest.min_play_s") == 45
    form["poll.interval_s"] = "1"
    form["ui.timezone"] = "Mars/Olympus"
    logged_in.post("/settings", form)
    assert settings.get(app_conn, "poll.interval_s") == 10
    assert settings.get(app_conn, "ui.timezone") == "Europe/London"


def test_image_proxy_only_allows_library_paths(logged_in):
    assert logged_in.get("/img/plex?path=/status/sessions").status_code == 404
    assert logged_in.get("/img/plex?path=http://evil/x.jpg").status_code == 404


@responses.activate
def test_image_proxy_fetches_and_caches(logged_in):
    responses.get("http://plex.test:32400/photo/:/transcode", body=b"\xff\xd8jpeg", content_type="image/jpeg")
    path = "/library/metadata/1/thumb/2"
    r1 = logged_in.get(f"/img/plex?path={path}&w=280")
    assert r1.status_code == 200 and r1.data == b"\xff\xd8jpeg"
    r2 = logged_in.get(f"/img/plex?path={path}&w=300")    # snaps to the same width -> cache hit
    assert r2.status_code == 200
    assert len(responses.calls) == 1
    assert responses.calls[0].request.headers["X-Plex-Token"] == "tok"


def test_api_health_is_public(app):
    r = app.test_client().get("/api/v1/health")
    assert r.status_code == 200 and r.json["ok"] is True


def test_api_activity_needs_auth(app, app_conn):
    c = app.test_client()
    assert c.get("/api/v1/activity").status_code == 401
    import hashlib
    app_conn.execute("INSERT INTO api_tokens (name, token_hash, scope, created_at) VALUES ('ha', ?, 'read', 0)",
                     (hashlib.sha256(b"secret-token").hexdigest(),))
    r = c.get("/api/v1/activity", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200 and r.json["totals"]["streams"] == 0
    r = c.post("/api/v1/activity/x/stop", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 403


@responses.activate
def test_manual_plex_connection(logged_in, app_conn):
    responses.get("http://192.168.1.10:32400/identity", json=load_fixture("identity.json"))
    responses.get("http://192.168.1.10:32400/", json=load_fixture("root.json"))
    responses.get("http://192.168.1.10:32400/status/sessions", json=load_fixture("sessions_empty.json"))
    logged_in.get("/setup/plex")
    r = logged_in.post("/setup/plex/save", {"url": "192.168.1.10", "token": "newtoken", "verify_tls": "1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/activity")
    assert settings.get(app_conn, "plex.url") == "http://192.168.1.10:32400"
    assert settings.get(app_conn, "plex.token") == "newtoken"


@responses.activate
def test_manual_plex_connection_bad_token(logged_in, app_conn):
    responses.get("http://192.168.1.10:32400/identity", status=401)
    logged_in.get("/setup/plex")
    r = logged_in.post("/setup/plex/save", {"url": "http://192.168.1.10:32400", "token": "bad"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/setup/plex")
    assert settings.get(app_conn, "plex.token") == "tok"


@responses.activate
def test_pin_flow(logged_in, app_conn):
    responses.post("https://plex.tv/api/v2/pins", json={"id": 42, "code": "abcd"})
    responses.get("https://plex.tv/api/v2/pins/42", json={"id": 42, "authToken": None})
    logged_in.get("/setup/plex")
    r = logged_in.post("/setup/plex/pin", headers={"HX-Request": "true"})
    assert b"app.plex.tv/auth" in r.data
    r = logged_in.get("/setup/plex/pin/42")
    assert b"Waiting for you" in r.data
    responses.replace(responses.GET, "https://plex.tv/api/v2/pins/42", json={"id": 42, "authToken": "pintoken"})
    responses.get("https://plex.tv/api/v2/resources", json=[
        {"name": "Home", "provides": "server", "owned": True, "clientIdentifier": "m1",
         "connections": [{"uri": "https://1-2-3-4.abc.plex.direct:32400", "address": "192.168.4.125",
                          "port": 32400, "local": True, "protocol": "https"}]}])
    r = logged_in.get("/setup/plex/pin/42")
    assert b"http://192.168.4.125:32400" in r.data
    assert b"pintoken" not in r.data
    assert settings.get(app_conn, "plex.pending_token") == "pintoken"
