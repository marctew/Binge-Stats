"""M8: theme, About page, the get-started step."""
from bingestats.services import jobs, settings


def test_theme_switch(logged_in, app_conn):
    body = logged_in.get("/").data.decode()
    assert 'data-theme="dark"' in body and 'aria-label="Light theme"' in body
    r = logged_in.post("/settings/theme", {"theme": "light", "next": "/history?user=1"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/history?user=1")
    assert settings.get(app_conn, "ui.theme") == "light"
    assert 'data-theme="light"' in logged_in.get("/").data.decode()
    logged_in.post("/settings/theme", {"theme": "system", "next": "https://evil.example"})
    body = logged_in.get("/").data.decode()
    assert settings.get(app_conn, "ui.theme") == "system" and 'prefers-color-scheme: light' in body
    logged_in.post("/settings/theme", {"theme": "neon"})
    assert settings.get(app_conn, "ui.theme") == "system"
    # the login page follows the theme too; Wrapped stays dark
    assert "prefers-color-scheme" in logged_in.client.application.test_client().get("/login").data.decode()


def test_about_page(logged_in, app_conn):
    app_conn.execute("INSERT INTO servers (id, machine_id, name, version, is_up) VALUES (1, 'm', 'Unraid Plex', '1.42.1', 1)")
    body = logged_in.get("/about").data.decode()
    assert "Database schema" in body and "Unraid Plex · 1.42.1" in body and "DB-IP" in body and "OpenStreetMap" in body


def test_get_started_progress(logged_in, app_conn):
    body = logged_in.get("/setup/start").data.decode()
    assert "Coming from Tautulli?" in body and "Get started" in body
    body = logged_in.get("/setup/start/progress").data.decode()
    assert "Waiting for the worker" in body or "isn’t running" in body
    app_conn.execute("INSERT INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'Plex', 1)")
    jid = jobs.create(app_conn, "library_sync")
    jobs.claim_next(app_conn)
    jobs.progress(app_conn, jid, 40, "Films: 1,200 of 3,000")
    body = logged_in.get("/setup/start/progress").data.decode()
    assert "Plex is up" in body and "Films: 1,200 of 3,000 · 40%" in body


def test_fresh_install_prompts_for_import(logged_in, app_conn):
    assert "Import from Tautulli" in logged_in.get("/").data.decode()
    assert "No library synced yet" in logged_in.get("/insights/library-health").data.decode()
    assert "No plays recorded on this server" in logged_in.get("/wrapped/2026/0").data.decode()
    app_conn.execute("INSERT INTO users (id, username, share_salt) VALUES (1, 'u', 's')")
    app_conn.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type) "
                     "VALUES (1, 1, 1, 2, 1, 'movie')")
    assert "No history yet" not in logged_in.get("/").data.decode()
