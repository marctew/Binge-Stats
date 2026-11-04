"""/healthz (homelab app standard) and the Design system card in Settings."""
import time


def test_healthz_is_public_and_lists_running_jobs(app, app_conn):
    client = app.test_client()
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "busy": []}
    app_conn.execute("INSERT INTO jobs (kind, status, created_at, started_at) VALUES ('tautulli_import', 'running', ?, ?)",
                     (int(time.time()), int(time.time())))
    app_conn.commit()
    assert client.get("/healthz").get_json()["busy"] == ["tautulli_import"]


def test_settings_shows_the_design_system_card(logged_in):
    html = logged_in.get("/settings").get_data(as_text=True)
    assert "data-design-card" in html and "Design system" in html and "data-csrf=" in html


def test_design_api_takes_the_csrf_header(logged_in):
    client = logged_in.client
    assert client.post("/api/design/check", json={}).status_code in (400, 403)
    r = client.post("/api/design/check", json={}, headers={"X-CSRF-Token": logged_in.csrf})
    assert r.status_code == 200 and r.get_json()["busy"] is True
