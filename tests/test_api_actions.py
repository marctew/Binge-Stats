"""The outside API standard, stage 3 (docs/specs/api-standard.md): named actions, Idempotency-Key,
and the action log on Settings → API tokens."""
import json

import pytest

from bingestats.services import api_actions, api_audit, api_tokens, pins
from tests.test_api_standard import bearer
from tests.test_pin_lists import build
from tests.test_pins import ALEX, OWNER


@pytest.fixture
def rw(app_conn):
    return api_tokens.create(app_conn, "PC panel", "write")


@pytest.fixture
def ro(app_conn):
    return api_tokens.create(app_conn, "Vital Signs desk", "read")


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def lib(app_conn):
    build(app_conn)
    app_conn.commit()
    return app_conn


def act(client, tok, name, body=None, **headers):
    return client.post(f"/api/v1/actions/{name}", headers={**bearer(tok), **headers},
                       data=json.dumps(body if body is not None else {}), content_type="application/json")


def audit(conn):
    return [dict(r) for r in conn.execute("SELECT token_name, action, status, message FROM api_audit ORDER BY id")]


# -- who may act ---------------------------------------------------------------------------------

def test_read_token_is_forbidden(client, ro, lib):
    r = act(client, ro, "ack_all_alerts")
    assert r.status_code == 403 and r.get_json()["code"] == "forbidden"


def test_write_token_in_the_url_is_refused(client, rw, lib):
    r = client.post(f"/api/v1/actions/ack_all_alerts?token={rw}", json={})
    assert r.status_code == 400 and "Authorization header" in r.get_json()["error"]


def test_no_token(client):
    assert client.post("/api/v1/actions/ack_all_alerts", json={}).status_code == 401


def test_admin_session_needs_csrf(logged_in):
    c = logged_in.client
    assert c.post("/api/v1/actions/ack_all_alerts", json={}).status_code == 400
    r = c.post("/api/v1/actions/ack_all_alerts", json={}, headers={"X-CSRF-Token": logged_in.csrf})
    assert r.status_code == 200 and r.get_json()["ok"] is True


# -- pins -----------------------------------------------------------------------------------------

def test_pin_and_unpin_show(client, rw, lib):
    r = act(client, rw, "pin_show", {"tvdb_id": 333, "user_id": ALEX})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["result"] == {"tvdb_id": 333, "user_id": ALEX, "pinned": True, "changed": True}
    assert body["message"].startswith("Pinned ") and pins.pinned(lib, ALEX) == {333}
    again = act(client, rw, "pin_show", {"tvdb_id": 333, "user_id": ALEX}).get_json()
    assert again["result"]["changed"] is False and again["message"].endswith("was already pinned.")
    off = act(client, rw, "unpin_show", {"tvdb_id": 333, "user_id": ALEX}).get_json()
    assert off["message"].startswith("Unpinned ") and pins.pinned(lib, ALEX) == set()
    assert act(client, rw, "unpin_show", {"tvdb_id": 333, "user_id": ALEX}).get_json()["message"].endswith("wasn't pinned.")


def test_pin_defaults_to_the_owner(client, rw, lib):
    assert act(client, rw, "pin_show", {"tvdb_id": 333}).get_json()["result"]["user_id"] == OWNER


@pytest.mark.parametrize("body,status", [
    ({}, 400), ({"tvdb_id": "abc"}, 400), ({"tvdb_id": True}, 400), ({"tvdb_id": 999999}, 404),
    ({"tvdb_id": 333, "user_id": 424242}, 404),
])
def test_pin_errors(client, rw, lib, body, status):
    r = act(client, rw, "pin_show", body)
    assert r.status_code == status and set(r.get_json()) == {"error", "code"}


def test_body_must_be_an_object(client, rw, lib):
    r = client.post("/api/v1/actions/pin_show", headers=bearer(rw), data="[1, 2]", content_type="application/json")
    assert r.status_code == 400
    r = client.post("/api/v1/actions/pin_show", headers=bearer(rw), data="not json", content_type="application/json")
    assert r.status_code == 400


def test_empty_body_is_an_empty_object(client, rw, lib):
    assert client.post("/api/v1/actions/ack_all_alerts", headers=bearer(rw)).status_code == 200


# -- alerts ----------------------------------------------------------------------------------------

def test_ack_alerts(client, rw, app_conn):
    for _ in range(3):
        app_conn.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (1, 'stream_limit', NULL, '{}')")
    app_conn.commit()
    r = act(client, rw, "ack_alert", {"alert_id": 1}).get_json()
    assert r == {"ok": True, "message": "Alert cleared.", "result": {"alert_id": 1}}
    assert act(client, rw, "ack_alert", {"alert_id": 99}).status_code == 404
    assert act(client, rw, "ack_all_alerts").get_json()["message"] == "Cleared 2 alerts."
    assert act(client, rw, "ack_all_alerts").get_json()["message"] == "No alerts to clear."


# -- move_download -------------------------------------------------------------------------------------

def test_move_download_is_off_and_unlisted_by_default(client, rw, ro):
    r = act(client, rw, "move_download", {"nzo_id": "SABnzbd_nzo_1", "to": "top"})
    assert r.status_code == 403 and "turned off" in r.get_json()["error"]
    names = [a["name"] for a in client.get("/api/v1/meta", headers=bearer(ro)).get_json()["actions"]]
    assert "move_download" not in names


def test_move_download_when_allowed(client, rw, ro, monkeypatch):
    from bingestats.services import arr
    monkeypatch.setattr(api_actions.ACTIONS["move_download"], "when", lambda conn: True)
    calls = []
    monkeypatch.setattr(arr, "move", lambda conn, nzo, to=None, above=None, below=None: calls.append((nzo, to)) or
                        {"moved": 1})
    monkeypatch.setattr(arr, "download_queue", lambda conn, now=None: {"ready": True, "items": [
        {"nzo_id": "SABnzbd_nzo_1", "title": "Slow Horses", "sub": "S05E03 · Missing Persons"}]})
    names = [a["name"] for a in client.get("/api/v1/meta", headers=bearer(ro)).get_json()["actions"]]
    assert "move_download" in names
    r = act(client, rw, "move_download", {"nzo_id": "SABnzbd_nzo_1", "to": "top"}).get_json()
    assert r["message"] == "Moved Slow Horses S05E03 to the top." and calls == [("SABnzbd_nzo_1", "top")]
    assert act(client, rw, "move_download", {"to": "top"}).status_code == 400


def test_move_download_upstream_failure_is_502(client, rw, monkeypatch):
    from bingestats.arr.client import ArrError
    from bingestats.services import arr
    monkeypatch.setattr(api_actions.ACTIONS["move_download"], "when", lambda conn: True)

    def boom(*a, **k):
        raise ArrError("SABnzbd said no")
    monkeypatch.setattr(arr, "move", boom)
    monkeypatch.setattr(arr, "download_queue", lambda conn, now=None: None)
    r = act(client, rw, "move_download", {"nzo_id": "SABnzbd_nzo_1", "to": "top"})
    assert r.status_code == 502 and r.get_json()["code"] == "upstream"


# -- unknown, idempotency -------------------------------------------------------------------------------

def test_unknown_action(client, rw):
    r = act(client, rw, "stop_session", {"session_id": "x"})
    assert r.status_code == 404 and "pin_show" in r.get_json()["error"] and "stop_session" not in api_actions.ACTIONS


def test_idempotency_key_does_it_once(client, rw, app_conn):
    for _ in range(2):
        app_conn.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (1, 'stream_limit', NULL, '{}')")
    app_conn.commit()
    first = act(client, rw, "ack_all_alerts", **{"Idempotency-Key": "panel-btn-1"})
    app_conn.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (1, 'stream_limit', NULL, '{}')")
    app_conn.commit()
    again = act(client, rw, "ack_all_alerts", **{"Idempotency-Key": "panel-btn-1"})
    assert again.status_code == 200 and again.get_json() == first.get_json() == \
        {"ok": True, "message": "Cleared 2 alerts.", "result": {"acknowledged": 2}}
    assert app_conn.execute("SELECT COUNT(*) FROM alerts WHERE acknowledged = 0").fetchone()[0] == 1   # not run twice
    assert act(client, rw, "ack_all_alerts", **{"Idempotency-Key": "panel-btn-2"}).get_json()["message"] == \
        "Cleared 1 alert."


def test_idempotency_key_is_per_token_and_per_action(client, rw, app_conn):
    other = api_tokens.create(app_conn, "Home Assistant", "write")
    act(client, rw, "ack_all_alerts", **{"Idempotency-Key": "k"})
    assert act(client, other, "ack_all_alerts", **{"Idempotency-Key": "k"}).status_code == 200
    r = act(client, rw, "ack_alert", {"alert_id": 1}, **{"Idempotency-Key": "k"})
    assert r.status_code == 400 and "different action" in r.get_json()["error"]


def test_idempotency_key_replays_refusals_too(client, rw, lib):
    a = act(client, rw, "pin_show", {"tvdb_id": 999999}, **{"Idempotency-Key": "x1"})
    b = act(client, rw, "pin_show", {"tvdb_id": 999999}, **{"Idempotency-Key": "x1"})
    assert a.status_code == b.status_code == 404 and a.get_json() == b.get_json()


def test_idempotency_key_expires_after_a_day(app_conn):
    for _ in range(2):
        app_conn.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (1, 'stream_limit', NULL, '{}')")
    t = 1_800_000_000
    api_actions.run(app_conn, "ack_alert", {"alert_id": 1}, token_id=5, token_name="x", key="k", now=t)
    status, out = api_actions.run(app_conn, "ack_alert", {"alert_id": 2}, token_id=5, token_name="x", key="k",
                                  now=t + 25 * 3600)
    assert status == 200 and out["result"] == {"alert_id": 2}
    assert api_actions.prune(app_conn, now=t + 50 * 3600) == 1


def test_idempotency_key_too_long(client, rw):
    assert act(client, rw, "ack_all_alerts", **{"Idempotency-Key": "k" * 101}).status_code == 400


# -- the action log ---------------------------------------------------------------------------------------

def test_every_call_is_logged_with_the_tokens_name(client, rw, ro, lib):
    act(client, rw, "pin_show", {"tvdb_id": 333})
    act(client, rw, "pin_show", {"tvdb_id": 999999})
    act(client, rw, "nonsense")
    rows = audit(lib)
    assert [(r["token_name"], r["action"], r["status"]) for r in rows] == [
        ("PC panel", "pin_show", 200), ("PC panel", "pin_show", 404), ("PC panel", "nonsense", 404)]
    assert rows[0]["message"].startswith("Pinned ")


def test_session_actions_are_logged_as_the_admin(logged_in, app_conn):
    logged_in.client.post("/api/v1/actions/ack_all_alerts", json={}, headers={"X-CSRF-Token": logged_in.csrf})
    assert audit(app_conn)[-1]["token_name"] == "Admin session"


def test_settings_shows_recent_actions(logged_in, app_conn, rw):
    html = logged_in.get("/settings").get_data(as_text=True)
    assert "Recent API actions" in html and "No actions yet." in html
    api_audit.record(app_conn, token_id=1, token_name="PC panel", action="pin_show", body={"tvdb_id": 1},
                     status=200, message="Pinned Slow Horses.")
    api_audit.record(app_conn, token_id=1, token_name="PC panel", action="ack_alert", body={},
                     status=404, message="There's no alert 9.")
    html = logged_in.get("/settings").get_data(as_text=True)
    assert "data-api-actions" in html and "Pinned Slow Horses." in html and "badge-danger" in html


def test_audit_prune(app_conn):
    api_audit.record(app_conn, token_id=1, token_name="x", action="a", body={}, status=200, message="m", now=1000)
    api_audit.record(app_conn, token_id=1, token_name="x", action="a", body={}, status=200, message="m",
                     now=1000 + 91 * 86400)
    assert api_audit.prune(app_conn, now=1000 + 91 * 86400) == 1


def test_audit_body_is_capped(app_conn):
    api_audit.record(app_conn, token_id=1, token_name="x", action="a", body={"q": "y" * 2000}, status=200, message="m")
    assert len(app_conn.execute("SELECT body FROM api_audit").fetchone()[0]) == api_audit.BODY_MAX


def test_no_destructive_actions():
    assert not {"stop_session", "stop_user", "delete", "restore"} & set(api_actions.ACTIONS)
