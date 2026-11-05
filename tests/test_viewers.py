"""Viewer logins: people on the server signing in to see only their own stats."""
import re

import pytest

from bingestats.services import accounts, auth, settings

ALEX, SAM, KID, GONE = 11, 22, 33, 44


@pytest.fixture
def people(app_conn):
    c = app_conn
    c.execute("INSERT INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'Plex', 1)")
    for uid, username, name, active in ((ALEX, "alexp", "Alex", 1), (SAM, "samr", "Sam", 1), (KID, None, "Kiddo", 1),
                                        (GONE, "gonegirl", "Gone", 0)):
        c.execute("INSERT INTO users (id, username, friendly_name, is_active, share_salt) VALUES (?, ?, ?, ?, 's')",
                  (uid, username, name, active))
    for uid, title, t in ((ALEX, "Alex Film", 1_790_000_000), (SAM, "Sam Secret Film", 1_790_000_100)):
        c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, title, ip_address, "
                  "player_title) VALUES (1, ?, ?, ?, 3600, 'movie', ?, '81.2.69.160', 'TV')", (uid, t, t + 3600, title))
    return c


def viewer(app, browser_cls, username="alexp", password="alex-password"):
    b = browser_cls(app.test_client())
    b.get("/login")
    return b, b.post("/login", {"username": username, "password": password})


@pytest.fixture
def alex(app, people, logged_in):
    from tests.conftest import Browser
    accounts.set_password(people, ALEX, "alex-password")
    b, r = viewer(app, Browser, "ALEXP")                       # usernames are case-insensitive
    assert r.status_code == 302 and r.headers["Location"].endswith("/me")
    b.get("/me")                                               # pick up the CSRF token
    return b


# -- the service -----------------------------------------------------------------------------

def test_who_can_have_a_login(people):
    auth.set_admin(people, "samr", "admin-password")
    assert accounts.can_have_login(people, ALEX) == (True, "")
    assert not accounts.can_have_login(people, KID)[0]        # managed Home user: no username
    assert not accounts.can_have_login(people, GONE)[0]       # no longer has access
    ok, why = accounts.can_have_login(people, SAM)
    assert not ok and "admin login" in why                    # would clash with the admin
    with pytest.raises(ValueError, match="at least 8"):
        accounts.set_password(people, ALEX, "short")
    with pytest.raises(ValueError):
        accounts.set_password(people, KID, "long-enough")


def test_check_and_lose_access(people):
    accounts.set_password(people, ALEX, "alex-password", now=100)
    assert accounts.check(people, "alexp", "wrong") is None
    login = accounts.check(people, " AlexP ", "alex-password", now=200)
    assert login["user_id"] == ALEX and accounts.get(people, ALEX)["last_login_at"] == 200
    assert accounts.session_still_valid(people, ALEX, 100)
    people.execute("UPDATE users SET is_active = 0 WHERE id = ?", (ALEX,))
    assert accounts.check(people, "alexp", "alex-password") is None
    assert not accounts.session_still_valid(people, ALEX, 100)


# -- what a viewer sees -----------------------------------------------------------------------

def test_viewer_sees_only_their_own(alex):
    body = alex.get("/me").data.decode()
    assert "Hi Alex" in body and "Alex Film" in body and "Sam Secret Film" not in body
    assert "Settings" not in body and "Notifications" not in body          # admin nav isn't there
    body = alex.get(f"/me/history?user={SAM}").data.decode()
    assert "Alex Film" in body and "Sam Secret Film" not in body
    d = alex.get(f"/me/data/timeseries?range=all&user_id={SAM}").get_json()
    assert sum(sum(s["data"]) for s in d["series"]) == 1.0                # Alex's one hour, not two
    assert "/item/" not in alex.get("/me/history").data.decode()            # no links into admin pages


@pytest.mark.parametrize("url", ["/", "/activity", "/history", "/users", f"/users/{SAM}", f"/users/{ALEX}",
                                 "/settings", "/alerts", "/graphs", "/insights/binges", "/insights/library-health",
                                 "/notifications", "/wrapped", f"/wrapped/2026/{SAM}", f"/wrapped/2026/{ALEX}",
                                 "/about", "/import", "/settings/backups/bingestats-20260101-000000.db",
                                 "/history.csv", "/insights/library-health.csv"])
def test_viewer_is_sent_home_from_admin_pages(alex, url):
    r = alex.get(url)
    assert r.status_code == 302 and r.headers["Location"].endswith("/me")


@pytest.mark.parametrize("url", ["/api/v1/history", "/api/v1/users", "/api/v1/stats/summary", "/api/v1/activity",
                                 "/api/v1/meta", "/api/v1/status",
                                 f"/api/v1/users/{SAM}/completion", "/api/v1/alerts"])
def test_viewer_has_no_api(alex, url):
    assert alex.get(url).status_code in (401, 403)


@pytest.mark.parametrize("url", ["/settings/theme", f"/users/{SAM}/stop", "/alerts/ack", f"/users/{ALEX}/login",
                                 f"/wrapped/{SAM}/revoke", "/settings/backups", "/activity/x/stop"])
def test_viewer_cant_post_admin_actions(alex, people, url):
    assert alex.post(url, {"password": "x" * 10, "password2": "x" * 10, "theme": "light"}).status_code == 403
    assert settings.get(people, "ui.theme") == "dark"


def test_viewer_images_and_parts(alex, people):
    r = alex.get(f"/img/user/{SAM}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/me")   # someone else's avatar: refused
    sam_play = people.execute("SELECT id FROM plays WHERE user_id = ?", (SAM,)).fetchone()[0]
    assert alex.get(f"/me/history/{sam_play}/parts").status_code == 404


def test_viewer_theme_is_their_own(alex, people):
    alex.post("/me/theme", {"theme": "light"})
    assert 'data-theme="light"' in alex.get("/me").data.decode()
    assert settings.get(people, "ui.theme") == "dark"


def test_viewer_wrapped_and_revoke(alex, people):
    body = alex.get("/me/wrapped/2026").data.decode()
    assert "Alex&#39;s year on Plex" in body and "Copy share link" in body
    link = re.search(r"/wrapped/s/[^\"']+", body).group(0)
    assert alex.client.application.test_client().get(link).status_code == 200
    alex.post("/me/wrapped/revoke", {"year": "2026"})
    assert alex.client.application.test_client().get(link).status_code == 404


def test_change_own_password(alex, app):
    from tests.conftest import Browser
    r = alex.post("/me/account", {"current": "nope", "new": "new-password", "again": "new-password"}, follow_redirects=True)
    assert b"current password" in r.data
    alex.post("/me/account", {"current": "alex-password", "new": "new-password", "again": "new-password"})
    assert alex.get("/me").status_code == 200                              # still signed in here
    assert viewer(app, Browser, "alexp", "new-password")[1].headers["Location"].endswith("/me")


def test_admin_reset_and_remove_sign_viewer_out(alex, logged_in, people):
    r = logged_in.post(f"/users/{ALEX}/login", {"password": "fresh-password", "password2": "fresh-password"})
    assert r.status_code == 302
    r = alex.get("/me")
    assert r.status_code == 302 and "/login" in r.headers["Location"]       # reset ended the old session
    logged_in.post(f"/users/{ALEX}/login/remove")
    assert accounts.get(people, ALEX) is None


# -- the admin side ---------------------------------------------------------------------------

def test_admin_creates_login_from_user_page(logged_in, people, app):
    from tests.conftest import Browser
    body = logged_in.get(f"/users/{SAM}").data.decode()
    assert "Binge Stats login" in body and "Create login" in body and "samr" in body
    r = logged_in.post(f"/users/{SAM}/login", {"password": "sam-password", "password2": "different!"}, follow_redirects=True)
    assert b"don&#39;t match" in r.data and accounts.get(people, SAM) is None
    logged_in.post(f"/users/{SAM}/login", {"password": "sam-password", "password2": "sam-password"})
    assert accounts.get(people, SAM) is not None
    assert "Login" in logged_in.get("/users").data.decode()
    assert "managed Home user" in logged_in.get(f"/users/{KID}").data.decode()
    assert viewer(app, Browser, "samr", "sam-password")[1].headers["Location"].endswith("/me")
    assert logged_in.get("/me").status_code == 404                          # the admin has no /me


def test_migration_7(tmp_path, monkeypatch):
    from bingestats import db
    c = db.connect(tmp_path / "v6.db")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:6])
    monkeypatch.setattr(db, "LATEST", 6)
    db.migrate(c)
    monkeypatch.undo()
    assert db.migrate(c) == db.LATEST
    assert c.execute("SELECT COUNT(*) FROM user_logins").fetchone()[0] == 0


def test_reset_in_the_same_second_still_ends_sessions(people):
    accounts.set_password(people, ALEX, "first-password", now=500)
    accounts.set_password(people, ALEX, "second-password", now=500)
    assert not accounts.session_still_valid(people, ALEX, 500)


def test_viewer_cannot_move_downloads(alex, app_conn):
    settings.set(app_conn, "sabnzbd.allow_moves", True)
    for url in ("/downloads/move", "/downloads/front"):
        r = alex.post(url, {"nzo_id": "SABnzbd_nzo_x", "to": "top"})
        assert r.status_code in (302, 403) and b'"ok"' not in r.data
