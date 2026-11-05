"""The pins REST API (docs/API.md) and the payload the Scriptable widget reads."""
import json
import time

import pytest

from bingestats.services import api_tokens, pin_api, pins
from tests.test_pin_lists import build
from tests.test_pins import ALEX, OWNER


@pytest.fixture
def api(app, app_conn):
    build(app_conn)
    ro = {"Authorization": f"Bearer {api_tokens.create(app_conn, 'widget', 'read')}"}
    rw = {"Authorization": f"Bearer {api_tokens.create(app_conn, 'script', 'write')}"}
    return app.test_client(), ro, rw, app_conn


def test_needs_a_token(api):
    c, *_ = api
    assert c.get("/api/v1/pins").status_code == 401
    assert c.get("/api/v1/pins/widget").status_code == 401


def test_pins_list_and_who(api):
    c, ro, _rw, conn = api
    body = c.get("/api/v1/pins", headers=ro).get_json()
    assert body["user_id"] == OWNER and [p["title"] for p in body["pins"]] == ["Done And Dusted", "Pinned Show", "Sonarr Only"]
    p = body["pins"][1]
    assert {"tvdb_id", "outlook", "outlook_label", "next_at", "next_local", "notify", "in_sonarr"} <= set(p)
    assert c.get(f"/api/v1/pins?user_id={ALEX}", headers=ro).get_json()["pins"] == []
    assert c.get("/api/v1/pins?user_id=424242", headers=ro).status_code == 404


def test_pin_and_unpin(api):
    c, ro, rw, conn = api
    assert c.put("/api/v1/pins/333", headers=ro).status_code == 403              # read-only token
    r = c.put(f"/api/v1/pins/333?user_id={ALEX}", headers=rw).get_json()
    assert r == {"tvdb_id": 333, "pinned": True, "changed": True} and pins.pinned(conn, ALEX) == {333}
    assert c.put(f"/api/v1/pins/333?user_id={ALEX}", headers=rw).get_json()["changed"] is False
    assert c.put("/api/v1/pins/999999", headers=rw).status_code == 404            # not in Plex or Sonarr
    assert c.delete(f"/api/v1/pins/333?user_id={ALEX}", headers=rw).get_json() == \
        {"tvdb_id": 333, "pinned": False, "changed": True}
    assert pins.pinned(conn, ALEX) == set()


def test_schedule_next_arrivals_lists(api):
    c, ro, _rw, conn = api
    sch = c.get("/api/v1/pins/schedule?days=60&back=30", headers=ro).get_json()["episodes"]
    assert sch and {"code", "air_at", "air_local", "state", "label", "stalled"} <= set(sch[0])
    nxt = c.get("/api/v1/pins/next", headers=ro).get_json()["next"]
    assert nxt is None or nxt["air_at"] >= int(time.time()) - 86400
    conn.execute("UPDATE media_items SET added_at = ? WHERE rating_key = 103", (int(time.time()) - 3600,))
    arr = c.get("/api/v1/pins/arrivals?hours=2", headers=ro).get_json()["episodes"]
    assert [(e["show"], e["code"]) for e in arr] == [("Pinned Show", "S01E03")]
    assert c.get("/api/v1/pins/ready", headers=ro).get_json()["ready"][0]["show"] == "Pinned Show"
    assert "retire" in c.get("/api/v1/pins/retire", headers=ro).get_json()
    assert "discover" in c.get("/api/v1/pins/discover", headers=ro).get_json()


def test_summary(api):
    c, ro, *_ = api
    s = c.get("/api/v1/pins/summary", headers=ro).get_json()
    assert s["user_id"] == OWNER and s["pins"] == 3
    assert s["ready"]["shows"] == 1 and s["ready"]["episodes"] == 2 and s["ready"]["top"][0]["next"] == "S01E02"
    assert set(s["overdue"]) == {"total", "missing"} and set(s["downloads"]) == {"jobs", "stalled"}
    assert isinstance(s["healthy"], bool)


def test_widget_payload(conn):
    from tests.test_pins import NOW
    build(conn)
    w = pin_api.widget(conn, OWNER, "https://binge.example.com/", now=NOW)   # Thursday noon; S01E04 airs at 14:00
    assert w["headline"] == "Pinned Show S01E04" and w["subhead"] == "14:00"
    assert w["next"] == {"series": "Pinned Show", "code": "S01E04", "title": "Tonight", "when": "14:00",
                         "state": "airing_today", "label": "Today", "poster": "/api/v1/pins/poster/111"}
    assert w["url"] == "https://binge.example.com/calendar" and w["tonight"] == 1
    stats = {s["label"]: s for s in w["stats"]}
    assert stats["Ready"]["value"] == "1" and stats["Ready"]["detail"] == "2 eps"
    assert stats["Missing"] == {"label": "Missing", "value": "0", "detail": "", "alert": False}  # S01E02 is in Plex here
    assert w["stale_after_minutes"] == 45 and json.dumps(w)                       # JSON-ready as it is
    for n in (5, 10):                                                   # two more at the same time: one headline
        conn.execute("INSERT OR REPLACE INTO sonarr_episodes VALUES (1, 1, ?, 'x', ?, 0, 1)", (n, int(NOW) + 2 * 3600))
    assert pin_api.widget(conn, OWNER, "", now=NOW)["headline"] == "Pinned Show S01E04–E10"
    nobody = pin_api.widget(conn, ALEX, "", now=NOW)
    assert (nobody["headline"], nobody["next"], nobody["url"]) == ("Nothing coming", None, None)


def test_widget_route_and_poster(api, monkeypatch):
    c, ro, *_ = api
    w = c.get(f"/api/v1/pins/widget?user_id={OWNER}", headers=ro).get_json()
    assert isinstance(w["headline"], str) and len(w["stats"]) == 4
    from bingestats.web import routes_img
    monkeypatch.setattr(routes_img, "plex_image_response", lambda src, shape, w: ("img:" + src, 200))
    r = c.get("/api/v1/pins/poster/111", headers=ro)
    assert r.status_code == 200 and r.data == b"img:/library/metadata/10/thumb/1"
    assert c.get("/api/v1/pins/poster/999999", headers=ro).status_code == 404
