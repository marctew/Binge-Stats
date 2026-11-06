"""Home screen icons and the manifest (Design Uniformity PWA-ICONS.md)."""
import io
import json

from PIL import Image

from bingestats.services import auth


def test_icons_load_without_signing_in(app, app_conn):
    auth.set_admin(app_conn, "marc", "correct horse")         # set up, but this client isn't signed in
    client = app.test_client()
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200 and r.mimetype == "image/png", name
        img = Image.open(io.BytesIO(r.data))
        assert img.size == (size, size) and img.mode == "RGB"   # opaque: no black corners on iOS
    ico = client.get("/static/favicon.ico")
    assert ico.status_code == 200 and Image.open(io.BytesIO(ico.data)).size == (32, 32)
    r = client.get("/favicon.ico")
    assert r.status_code == 301 and r.headers["Location"].endswith("/static/favicon.ico")


def test_favicon_before_setup(app):
    assert app.test_client().get("/favicon.ico").status_code == 301


def test_manifest(app, app_conn):
    auth.set_admin(app_conn, "marc", "correct horse")
    r = app.test_client().get("/static/manifest.json")
    assert r.status_code == 200 and r.mimetype in ("application/json", "application/manifest+json")
    m = json.loads(r.data)
    assert m["name"] == m["short_name"] == "Binge Stats" and m["display"] == "standalone"
    assert m["background_color"] == m["theme_color"] == "#090b11"
    assert {i["src"] for i in m["icons"]} == {"/static/icon-192.png", "/static/icon-512.png"}
    assert any(i.get("purpose") == "maskable" for i in m["icons"])


def test_head_tags_on_the_app_and_the_login_page(app, logged_in):
    pages = [logged_in.get("/").get_data(as_text=True), app.test_client().get("/login").get_data(as_text=True)]
    for html in pages:
        assert 'rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=' in html
        assert '<meta name="apple-mobile-web-app-title" content="Binge Stats">' in html
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in html
        assert 'rel="manifest" href="/static/manifest.json?v=' in html
        assert 'href="/static/img/icon.svg"' in html and 'href="/static/favicon.ico" sizes="32x32"' in html
        assert html.count('<meta name="theme-color"') == 1      # only the one from theme_head.html
    assert 'class="topbar-safe ' in pages[0]                    # the top bar clears the notch (PWA-ICONS.md 3)
