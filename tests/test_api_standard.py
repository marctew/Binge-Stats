"""The outside API standard, stage 1 (docs/specs/api-standard.md): the shared gate in api_core
(tokens, URL tokens, lockout, rate limit, API_PUBLIC, ETags), /api/v1/meta and /api/v1/status."""
import json
import re
import time
from pathlib import Path

import pytest

from bingestats import db
from bingestats.plex import parse
from bingestats.services import api_feeds, api_tokens, settings
from bingestats.web import api_core

from tests.conftest import Clock, load_fixture


@pytest.fixture
def read_token(app_conn):
    return api_tokens.create(app_conn, "Vital Signs desk", "read")


@pytest.fixture
def write_token(app_conn):
    return api_tokens.create(app_conn, "PC panel", "write")


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def core(app):
    return app.extensions["du_api"]


@pytest.fixture
def clock(core):
    c = Clock(1_800_000_000)
    core.limiter.clock = c
    return c


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def healthy(conn, now=None):
    """Plex set up and up, the worker alive: status is ok unless a test changes something."""
    now = now or time.time()
    settings.set(conn, "plex.url", "http://plex.test:32400")
    settings.set(conn, "plex.token", "tok")
    settings.set(conn, "worker.heartbeat_at", int(now))
    conn.execute("INSERT OR REPLACE INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'DS9', 1)")
    conn.commit()


def play(conn, fixture, session_id="s1", user_id=1001, name="Marc"):
    s = parse.parse_sessions(load_fixture(fixture))[0][0]
    conn.execute("INSERT OR IGNORE INTO users (id, username, friendly_name, share_salt) VALUES (?, ?, ?, 'x')",
                 (user_id, name.lower(), name))
    conn.execute("INSERT INTO active_sessions (session_id, server_id, user_id, media_type, started_at, last_seen_at, "
                 "state, state_since, view_offset_s, duration_s, snapshot) VALUES (?, 1, ?, ?, 1, 1, 'playing', 1, ?, ?, ?)",
                 (session_id, user_id, s.media_type, s.view_offset_s, s.duration_s,
                  json.dumps({"session": s.to_dict()})))
    conn.commit()
    return s


# -- tokens -------------------------------------------------------------------------------------

def test_no_token_is_refused_in_the_standard_shape(client):
    r = client.get("/api/v1/status")
    assert r.status_code == 401
    assert r.get_json()["code"] == "unauthorised" and isinstance(r.get_json()["error"], str)


def test_header_token_reads(client, read_token):
    assert client.get("/api/v1/meta", headers=bearer(read_token)).status_code == 200


def test_read_token_in_the_url_reads(client, read_token):
    assert client.get(f"/api/v1/status?token={read_token}").status_code == 200


def test_url_read_token_works_on_binges_own_routes_too(client, read_token):
    assert client.get(f"/api/v1/pins/summary?token={read_token}").status_code in (200, 404)  # 404: no Plex owner yet
    assert client.get(f"/api/v1/libraries?token={read_token}").status_code == 200


def test_write_token_in_the_url_is_refused(client, write_token):
    r = client.get(f"/api/v1/status?token={write_token}")
    assert r.status_code == 400
    assert r.get_json() == {"error": "Write tokens go in the Authorization header, never the URL.", "code": "bad_request"}


def test_write_token_in_the_url_on_an_old_route_keeps_the_old_shape(client, write_token):
    r = client.get(f"/api/v1/libraries?token={write_token}")
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "bad_request"


def test_bad_token(client):
    r = client.get("/api/v1/status", headers=bearer("bs_nope"))
    assert r.status_code == 401
    assert r.get_json()["error"] == "That token isn't valid. Make a new one in Settings → API tokens."


def test_bad_token_on_an_old_route_keeps_the_old_shape(client):
    """The Scriptable widget reads error.message."""
    r = client.get("/api/v1/pins/summary", headers=bearer("bs_nope"))
    assert r.status_code == 401 and set(r.get_json()["error"]) == {"code", "message"}


def test_admin_session_reads_without_a_token(logged_in):
    assert logged_in.get("/api/v1/status").status_code == 200
    assert logged_in.get("/api/v1/meta").status_code == 200


def test_health_needs_no_token_even_with_a_bad_one(client):
    assert client.get("/api/v1/health", headers=bearer("bs_nope")).status_code == 200


def test_old_write_route_still_wants_a_write_token(client, read_token, app_conn):
    r = client.post("/api/v1/alerts/ack", headers=bearer(read_token))
    assert r.status_code == 403 and r.get_json()["error"]["code"] == "forbidden"


def test_old_write_route_from_a_session_still_wants_csrf(logged_in):
    c = logged_in.client
    assert c.post("/api/v1/alerts/ack").status_code == 400
    assert c.post("/api/v1/alerts/ack", headers={"X-CSRF-Token": logged_in.csrf}).status_code == 200


# -- lockout and rate limit ------------------------------------------------------------------------

def test_ten_bad_tokens_in_a_minute_lock_the_address_out(client, read_token, clock):
    for i in range(9):
        assert client.get("/api/v1/status", headers=bearer(f"bs_bad{i}")).status_code == 401
        clock.advance(1)
    r = client.get("/api/v1/status", headers=bearer("bs_bad9"))
    assert r.status_code == 429 and r.get_json()["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) == 15 * 60
    # even a good token from that address waits out the lockout
    assert client.get("/api/v1/status", headers=bearer(read_token)).status_code == 429
    clock.advance(15 * 60 + 1)
    assert client.get("/api/v1/status", headers=bearer(read_token)).status_code == 200


def test_bad_tokens_spread_out_dont_lock_out(client, clock):
    for i in range(20):
        assert client.get("/api/v1/status", headers=bearer(f"bs_bad{i}")).status_code == 401
        clock.advance(7)


def test_lockout_is_per_address(client, read_token, clock):
    for i in range(10):
        client.get("/api/v1/status", headers=bearer(f"bs_bad{i}"))
    other = client.get("/api/v1/status", headers=bearer(read_token), environ_base={"REMOTE_ADDR": "10.0.0.9"})
    assert other.status_code == 200


def test_lockout_through_the_tunnel_is_per_real_client(client, read_token, clock):
    """Every tunnel request comes from cloudflared's address; one stranger mustn't lock out the rest."""
    for i in range(10):
        client.get("/api/v1/status", headers={**bearer(f"bs_bad{i}"), "CF-Connecting-IP": "198.51.100.7"})
    assert client.get("/api/v1/status", headers={**bearer(read_token), "CF-Connecting-IP": "198.51.100.7"}).status_code == 429
    assert client.get("/api/v1/status", headers={**bearer(read_token), "CF-Connecting-IP": "203.0.113.20"}).status_code == 200


def test_rate_limit_per_token(client, read_token, write_token, clock):
    for _ in range(120):
        assert client.get("/api/v1/meta", headers=bearer(read_token)).status_code == 200
    r = client.get("/api/v1/meta", headers=bearer(read_token))
    assert r.status_code == 429 and 0 < int(r.headers["Retry-After"]) <= 61
    assert client.get("/api/v1/meta", headers=bearer(write_token)).status_code == 200  # another token is fine
    clock.advance(61)
    assert client.get("/api/v1/meta", headers=bearer(read_token)).status_code == 200


# -- API_PUBLIC ----------------------------------------------------------------------------------------

def test_api_public_off_refuses_the_tunnel(client, core, read_token):
    core.public = False
    tunnel = {**bearer(read_token), "CF-Connecting-IP": "203.0.113.9"}
    r = client.get("/api/v1/status", headers=tunnel)
    assert r.status_code == 403 and r.get_json()["code"] == "forbidden"
    assert client.get("/api/v1/status", headers=bearer(read_token)).status_code == 200


def test_api_public_on_by_default(client, core, read_token):
    assert core.public is True
    assert client.get("/api/v1/status", headers={**bearer(read_token), "CF-Connecting-IP": "203.0.113.9"}).status_code == 200


def test_api_public_reads_the_env(monkeypatch, tmp_path):
    from bingestats import create_app
    monkeypatch.setenv("API_PUBLIC", "0")
    assert create_app(str(tmp_path / "d"), testing=True).extensions["du_api"].public is False


# -- ETags ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["/api/v1/meta", "/api/v1/status", "/api/v1/libraries"])
def test_etag_and_304(client, read_token, url):
    r = client.get(url, headers=bearer(read_token))
    etag = r.headers["ETag"]
    assert etag.startswith("W/")
    again = client.get(url, headers={**bearer(read_token), "If-None-Match": etag})
    assert again.status_code == 304 and again.data == b""


def test_status_cache_control(client, read_token):
    r = client.get("/api/v1/status", headers=bearer(read_token))
    assert r.headers["Cache-Control"] == "private, max-age=7"


def test_ui_pages_get_no_api_etag(logged_in):
    assert "ETag" not in logged_in.get("/settings").headers


# -- meta ----------------------------------------------------------------------------------------------

def test_meta(client, read_token, app_conn):
    settings.set(app_conn, "ui.timezone", "America/New_York")
    app_conn.commit()
    m = client.get("/api/v1/meta", headers=bearer(read_token)).get_json()
    assert m["app"] == "binge-stats" and m["name"] == "Binge Stats" and m["api"] == 1
    assert m["accent"] == "violet" and m["time_zone"] == "America/New_York"
    assert re.match(r"^\d+\.\d+\.\d+", m["version"]) and "sha" in m
    assert m["feeds"] == [] and m["actions"] == [] and m["displays"] == [] and m["events"] == []


def test_meta_lists_only_what_is_registered_and_allowed(client, core, read_token):
    allowed = {"on": False}
    core.add_feed("arrivals", "Just arrived", 300, {"kind": "movie | show"})
    core.add_action("move_download", "Move a download", {"nzo_id": "str"}, when=lambda: allowed["on"])
    core.add_events(["arrival", "arrival", "playback.start"])
    m = client.get("/api/v1/meta", headers=bearer(read_token)).get_json()
    assert [f["name"] for f in m["feeds"]] == ["arrivals"]
    assert m["actions"] == [] and m["events"] == ["arrival", "playback.start"]
    allowed["on"] = True
    m = client.get("/api/v1/meta", headers=bearer(read_token)).get_json()
    assert m["actions"] == [{"name": "move_download", "title": "Move a download", "body": {"nzo_id": "str"}}]


# -- status ------------------------------------------------------------------------------------------

def status(client, token):
    r = client.get("/api/v1/status", headers=bearer(token))
    assert r.status_code == 200
    return r.get_json()


def counts(s):
    return {c["key"]: c["value"] for c in s["counts"]}


def test_status_shape(client, read_token, app_conn):
    healthy(app_conn)
    s = status(client, read_token)
    assert s["app"] == "binge-stats" and s["state"] == "ok" and s["headline"] == "Nothing playing"
    assert [c["key"] for c in s["counts"]] == ["streams", "transcodes", "bandwidth_mbps", "alerts", "downloads",
                                               "pins_ready"]
    assert all(set(c) <= {"key", "label", "value", "tone"} for c in s["counts"])
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", s["generated_at"])
    assert s["refresh_s"] == 15 and s["url"] == "http://localhost/"
    assert len(json.dumps(s)) < 2048


def test_status_uses_the_public_url(client, read_token, app_conn):
    healthy(app_conn)
    settings.set(app_conn, "ui.public_url", "https://binge.example.com")
    app_conn.commit()
    assert status(client, read_token)["url"] == "https://binge.example.com/"


def test_status_not_set_up(client, read_token):
    s = status(client, read_token)
    assert s["state"] == "error" and s["headline"] == "Plex isn't set up yet"


def test_status_plex_down(client, read_token, app_conn):
    healthy(app_conn)
    app_conn.execute("UPDATE servers SET is_up = 0")
    app_conn.commit()
    assert status(client, read_token)["headline"] == "Plex is down"


def test_status_worker_not_running(client, read_token, app_conn):
    healthy(app_conn, now=time.time() - 300)
    s = status(client, read_token)
    assert s["state"] == "error" and s["headline"] == "The worker isn't running"


def test_status_streaming(client, read_token, app_conn):
    healthy(app_conn)
    play(app_conn, "sessions_direct_play.json", "s1")
    play(app_conn, "sessions_transcode.json", "s2", user_id=1002, name="Sarah")
    s = status(client, read_token)
    assert s["state"] == "ok" and s["headline"] == "2 streams, 1 transcoding"
    assert s["detail"].startswith("Marc · ") and s["detail"].endswith("%")
    c = counts(s)
    assert c["streams"] == 2 and c["transcodes"] == 1 and isinstance(c["bandwidth_mbps"], float)


def test_status_warning_for_alerts_and_failing_apps(client, read_token, app_conn):
    healthy(app_conn)
    app_conn.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (1, 'stream_limit', 1, '{}')")
    app_conn.execute("INSERT INTO arr_status (app, ok) VALUES ('sonarr', 0)")
    app_conn.execute("INSERT INTO arr_status (app, ok) VALUES ('sabnzbd', 0)")
    app_conn.commit()
    s = status(client, read_token)
    assert s["state"] == "warning" and s["headline"] == "Nothing playing"
    assert s["detail"] == "1 alert to look at; SABnzbd and Sonarr didn't answer last time"
    alert = next(c for c in s["counts"] if c["key"] == "alerts")
    assert alert == {"key": "alerts", "label": "Alerts", "value": 1, "tone": "warning"}


def test_status_busy_during_an_import(client, read_token, app_conn):
    healthy(app_conn)
    app_conn.execute("INSERT INTO jobs (kind, status, created_at, started_at) VALUES ('tautulli_import', 'running', 1, 1)")
    app_conn.commit()
    s = status(client, read_token)
    assert s["state"] == "busy" and s["detail"] == "Running: tautulli import"


def test_status_counts_downloads_from_the_sab_queue(client, read_token, app_conn):
    healthy(app_conn)
    info = {"live": {"rows": [{"kind": "job", "nzo_id": "a"}, {"kind": "other", "count": 40},
                              {"kind": "job", "nzo_id": "b"}]}}
    app_conn.execute("INSERT INTO arr_status (app, ok, info) VALUES ('sabnzbd', 1, ?)", (json.dumps(info),))
    app_conn.commit()
    assert counts(status(client, read_token))["downloads"] == 2


def test_status_never_carries_the_plex_token(client, read_token, app_conn):
    healthy(app_conn)
    settings.set(app_conn, "plex.token", "SECRET-PLEX-TOKEN")
    app_conn.commit()
    play(app_conn, "sessions_transcode.json")
    assert b"SECRET-PLEX-TOKEN" not in client.get("/api/v1/status", headers=bearer(read_token)).data


def test_status_answers_from_the_database_only(app_conn, monkeypatch):
    """No Plex or *arr call in a request: anything that tried to build a client would blow up here."""
    from bingestats.services import activity
    healthy(app_conn)
    monkeypatch.setattr(activity, "plex_client", lambda conn: (_ for _ in ()).throw(AssertionError("called Plex")))
    assert api_feeds.status(app_conn, "http://x/")["state"] == "ok"


# -- iso helpers ---------------------------------------------------------------------------------

def test_iso_round_trip():
    assert api_core.iso(1_800_000_000) == "2027-01-15T08:00:00Z"
    assert api_core.parse_iso("2027-01-15T08:00:00Z") == 1_800_000_000
    assert api_core.parse_iso("2027-01-15T09:00:00+01:00") == 1_800_000_000
    assert api_core.parse_iso("yesterday") is None


# -- the migration and the log format -------------------------------------------------------------

def test_m14_upgrades_an_m13_database_and_keeps_tokens(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:13])
    monkeypatch.setattr(db, "LATEST", 13)
    c = db.connect(path)
    db.migrate(c)
    tok = api_tokens.create(c, "Old widget", "read")
    c.commit()
    monkeypatch.undo()
    assert db.migrate(c) == db.LATEST == 14
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"events", "webhooks", "webhook_deliveries", "api_idempotency", "api_audit"} <= tables
    assert api_tokens.check(c, tok)["name"] == "Old widget"
    c.close()


def test_access_log_never_has_query_strings_or_referrers():
    sh = (Path(__file__).parent.parent / "docker" / "entrypoint.sh").read_text()
    fmt = re.search(r"--access-logformat '([^']+)'", sh)
    assert fmt, "gunicorn needs an explicit --access-logformat"
    for atom in ("%(r)s", "%(q)s", "%(f)s", "%(u)s"):
        assert atom not in fmt.group(1)
    assert "%(U)s" in fmt.group(1)


def test_api_core_imports_nothing_from_binge():
    src = (Path(api_core.__file__)).read_text()
    assert not re.search(r"^\s*(from|import)\s+(\.\.|bingestats|\.)", src, re.M)
