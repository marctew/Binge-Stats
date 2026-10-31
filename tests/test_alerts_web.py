"""M5 pages and API: user limits, stop a user's streams, alerts."""
import json

import pytest

from bingestats.plex import client as client_mod
from bingestats.services import api_tokens


@pytest.fixture
def seeded(app_conn):
    c = app_conn
    c.execute("INSERT INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'Plex', 1)")
    c.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (1001, 'sarah_p', 'Sarah', 's')")
    for sid in ("s1", "s2"):
        c.execute("INSERT INTO active_sessions (session_id, server_id, user_id, media_type, started_at, last_seen_at, "
                  "state, state_since, snapshot) VALUES (?, 1, 1001, 'movie', 1, 1, 'playing', 1, '{}')", (sid,))
    c.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (1700000000, 'stream_limit', 1001, ?)",
              (json.dumps({"count": 3, "limit": 2, "stopped": "s3"}),))
    c.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (1700000100, 'new_country', 1001, ?)",
              (json.dumps({"country": "Spain", "country_code": "ES", "city": "Malaga"}),))
    return c


@pytest.fixture
def terminated(monkeypatch):
    calls = []
    monkeypatch.setattr(client_mod.PlexClient, "terminate", lambda self, sid, reason: calls.append((sid, reason)))
    return calls


def test_user_limits_save_and_validate(logged_in, seeded):
    logged_in.post("/users/1001", {"friendly_name": "Sarah", "max_streams": "2", "max_transcodes": "1",
                                   "limit_action": "stop", "limit_message": " Too many "})
    row = seeded.execute("SELECT max_streams, max_transcodes, limit_action, limit_message FROM users WHERE id = 1001").fetchone()
    assert tuple(row) == (2, 1, "stop", "Too many")
    r = logged_in.post("/users/1001", {"friendly_name": "Sarah", "max_streams": "50", "limit_action": "notify"},
                       follow_redirects=True)
    assert b"between 1 and 20" in r.data
    assert seeded.execute("SELECT max_streams FROM users WHERE id = 1001").fetchone()[0] == 2
    logged_in.post("/users/1001", {"friendly_name": "Sarah", "max_streams": "", "max_transcodes": "",
                                   "limit_action": "notify"})
    row = seeded.execute("SELECT max_streams, max_transcodes, limit_action FROM users WHERE id = 1001").fetchone()
    assert tuple(row) == (None, None, "notify")
    body = logged_in.get("/users/1001").data.decode()
    assert "When over the limit" in body and "for the first time" in body


def test_stop_user_streams(logged_in, seeded, terminated):
    r = logged_in.post("/users/1001/stop", {"message": "Bedtime"}, headers={"HX-Request": "true"})
    assert b"Stopped 2 streams" in r.data
    assert sorted(terminated) == [("s1", "Bedtime"), ("s2", "Bedtime")]
    seeded.execute("DELETE FROM active_sessions")
    r = logged_in.post("/users/1001/stop", {}, headers={"HX-Request": "true"})
    assert b"Nothing playing" in r.data


def test_alerts_page_ack_and_dashboard(logged_in, seeded):
    seeded.execute("DELETE FROM active_sessions")
    body = logged_in.get("/alerts").data.decode()
    assert "Sarah had 3 streams at once (limit 2). The newest was stopped." in body
    assert "Sarah played from Malaga, Spain for the first time." in body
    assert "Needs a look" in logged_in.get("/").data.decode()
    aid = seeded.execute("SELECT id FROM alerts WHERE kind = 'new_country'").fetchone()[0]
    r = logged_in.post("/alerts/ack", {"id": str(aid), "next": "/alerts"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/alerts")
    body = logged_in.get("/alerts").data.decode()
    assert "Malaga" not in body and "Malaga" in logged_in.get("/alerts?all=1").data.decode()
    r = logged_in.post("/alerts/ack", {"next": "https://evil.example/"})
    assert r.headers["Location"].startswith("/") and "evil" not in r.headers["Location"]
    assert seeded.execute("SELECT COUNT(*) FROM alerts WHERE acknowledged = 0").fetchone()[0] == 0
    assert "Needs a look" not in logged_in.get("/").data.decode()


def test_api_alerts_and_stop(app, app_conn, seeded, terminated):
    from bingestats.services import settings
    settings.set(app_conn, "plex.url", "http://plex.test:32400")
    settings.set(app_conn, "plex.token", "tok")
    c = app.test_client()
    ro = {"Authorization": f"Bearer {api_tokens.create(app_conn, 'ha', 'read')}"}
    rw = {"Authorization": f"Bearer {api_tokens.create(app_conn, 'script', 'write')}"}

    d = c.get("/api/v1/alerts", headers=ro).get_json()
    assert d["unacknowledged"] == 2 and [a["kind"] for a in d["items"]] == ["new_country", "stream_limit"]
    assert d["items"][0]["text"].startswith("Sarah played from Malaga")
    aid = d["items"][0]["id"]
    assert c.post(f"/api/v1/alerts/{aid}/ack", headers=ro).status_code == 403
    assert c.post(f"/api/v1/alerts/{aid}/ack", headers=rw).get_json() == {"ok": True}
    assert c.post("/api/v1/alerts/9999/ack", headers=rw).status_code == 404
    assert len(c.get("/api/v1/alerts", headers=ro).get_json()["items"]) == 1
    assert len(c.get("/api/v1/alerts?all=1&user_id=1001", headers=ro).get_json()["items"]) == 2
    assert c.post("/api/v1/alerts/ack", headers=rw).get_json() == {"ok": True, "acknowledged": 1}

    assert c.post("/api/v1/users/1001/stop", json={"message": "Night"}, headers=rw).get_json() == {"ok": True, "stopped": 2}
    assert sorted(terminated) == [("s1", "Night"), ("s2", "Night")]
    assert c.post("/api/v1/users/4242/stop", headers=rw).status_code == 404

    r = c.patch("/api/v1/users/1001", json={"max_streams": 3, "limit_action": "stop"}, headers=rw)
    assert r.get_json() == {"ok": True}
    row = app_conn.execute("SELECT friendly_name, max_streams, limit_action FROM users WHERE id = 1001").fetchone()
    assert tuple(row) == ("Sarah", 3, "stop")
    assert c.patch("/api/v1/users/1001", json={"limit_action": "nuke"}, headers=rw).status_code == 400
