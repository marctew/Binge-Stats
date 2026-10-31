import pytest

from bingestats.services import api_tokens


@pytest.fixture
def seeded(app_conn):
    import time
    now = int(time.time())
    c = app_conn
    c.execute("INSERT INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'DS9', 1)")
    c.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (1, 'sarah', 'Sarah', 'secretsalt')")
    for i in range(3):
        c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, title, "
                  "grandparent_title, grandparent_guid, decision, product, library_id) VALUES (1, 1, ?, ?, 1800, "
                  "'episode', 'Ep', 'Slow Horses', 'plex://show/sh', 'direct_play', 'Plex for LG', 5)",
                  (now - 3600 * (i + 2), now - 3600 * (i + 2) + 1800))
    return c


@pytest.fixture
def token(app_conn):
    return api_tokens.create(app_conn, "Home Assistant", "read")


def get(app, url, token):
    return app.test_client().get(url, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("url", [
    "/api/v1/stats/summary?range=7d", "/api/v1/stats/timeseries?range=30d&split=media_type",
    "/api/v1/stats/by?dim=hour", "/api/v1/stats/heatmap", "/api/v1/stats/breakdown?field=product",
    "/api/v1/stats/top?kind=shows", "/api/v1/stats/concurrent", "/api/v1/stats/resolution",
    "/api/v1/history?limit=2", "/api/v1/users", "/api/v1/users/1", "/api/v1/users/1/stats",
    "/api/v1/libraries", "/api/v1/activity",
])
def test_read_endpoints(app, seeded, token, url):
    r = get(app, url, token)
    assert r.status_code == 200, (url, r.json)
    assert r.is_json


def test_stats_values(app, seeded, token):
    s = get(app, "/api/v1/stats/summary?range=7d", token).json
    assert s["plays"] == 3 and s["watch_time_s"] == 5400
    top = get(app, "/api/v1/stats/top?kind=shows&range=7d", token).json["items"]
    assert top[0]["name"] == "Slow Horses" and top[0]["plays"] == 3


def test_history_pagination_and_no_secrets(app, seeded, token):
    r = get(app, "/api/v1/history?limit=2", token).json
    assert len(r["items"]) == 2 and r["next_cursor"]
    r2 = get(app, f"/api/v1/history?limit=2&cursor={r['next_cursor']}", token).json
    assert len(r2["items"]) == 1 and r2["next_cursor"] is None
    assert "share_salt" not in get(app, "/api/v1/users/1", token).json
    assert all("share_salt" not in u for u in get(app, "/api/v1/users", token).json["items"])


def test_not_found_and_auth(app, seeded, token):
    assert get(app, "/api/v1/users/999", token).status_code == 404
    assert get(app, "/api/v1/stats/summary", "bs_wrong").status_code == 401
    assert app.test_client().get("/api/v1/stats/summary").status_code == 401


def test_patch_user_needs_write(app, seeded, app_conn, token):
    c = app.test_client()
    r = c.patch("/api/v1/users/1", json={"friendly_name": "Saz"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    w = api_tokens.create(app_conn, "script", "write")
    r = c.patch("/api/v1/users/1", json={"friendly_name": "Saz"}, headers={"Authorization": f"Bearer {w}"})
    assert r.status_code == 200
    assert app_conn.execute("SELECT friendly_name FROM users WHERE id = 1").fetchone()[0] == "Saz"


def test_session_cookie_reads_api(logged_in, seeded):
    r = logged_in.client.get("/api/v1/stats/summary?range=7d")
    assert r.status_code == 200 and r.json["plays"] == 3


def test_dashboard_and_graphs_render_with_data(logged_in, seeded):
    body = logged_in.get("/").data.decode()
    assert "Slow Horses" in body and "Sarah" in body and "data-chart" in body
    body = logged_in.get("/graphs?range=7d&metric=watch_time").data.decode()
    assert "metric=watch_time" in body and "Hours watched by type" in body
    assert logged_in.get("/graphs?range=bogus").status_code == 200


def test_tokens_hashed_and_revocable(app_conn):
    t = api_tokens.create(app_conn, "x", "read")
    assert t.startswith("bs_")
    assert app_conn.execute("SELECT token_hash FROM api_tokens").fetchone()[0] != t
    tid = api_tokens.list_tokens(app_conn)[0]["id"]
    api_tokens.revoke(app_conn, tid)
    assert api_tokens.check(app_conn, t) is None
    with pytest.raises(ValueError):
        api_tokens.create(app_conn, "", "read")


def test_token_ui_shows_once(logged_in, app_conn):
    r = logged_in.post("/settings/tokens", {"name": "HA", "scope": "read"})
    body = r.data.decode()
    assert r.status_code == 200 and "be shown again" in body
    import re
    tok = re.search(r'value="(bs_[^"]+)"', body).group(1)
    assert api_tokens.check(app_conn, tok)["name"] == "HA"
    assert tok not in logged_in.get("/settings").data.decode()
    tid = api_tokens.list_tokens(app_conn)[0]["id"]
    logged_in.post(f"/settings/tokens/{tid}/delete", {})
    assert api_tokens.check(app_conn, tok) is None
