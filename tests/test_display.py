"""The outside API standard, stage 5 (docs/specs/api-standard.md): the /display/now-playing page."""
import re

import pytest

from bingestats.services import api_tokens
from tests.test_api_feeds import add_item
from tests.test_api_standard import healthy, play
from tests.test_pins import NOW


@pytest.fixture
def tok(app_conn):
    return api_tokens.create(app_conn, "Kitchen kiosk", "read")


@pytest.fixture
def client(app):
    return app.test_client()


def page(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return r, r.get_data(as_text=True)


def test_needs_a_token_and_says_so_as_a_page(client):
    r = client.get("/display/now-playing")
    assert r.status_code == 401 and r.mimetype == "text/html"
    assert "needs a read token" in r.get_data(as_text=True)
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_bad_token_is_a_page_not_json(client):
    r = client.get("/display/now-playing?token=bs_nope")
    assert r.status_code == 401 and r.mimetype == "text/html" and "isn't valid" in r.get_data(as_text=True)


def test_write_token_in_the_url_is_refused(client, app_conn):
    w = api_tokens.create(app_conn, "panel", "write")
    r = client.get(f"/display/now-playing?token={w}")
    assert r.status_code == 400 and r.mimetype == "text/html"


def test_idle_page_with_arrivals(client, tok, app_conn):
    healthy(app_conn)
    add_item(app_conn, 600, "movie", "Rumours", NOW, year=1977, thumb="/library/metadata/600/thumb/1")
    app_conn.commit()
    r, html = page(client, f"/display/now-playing?token={tok}")
    assert r.headers["Referrer-Policy"] == "no-referrer" and r.headers["Cache-Control"] == "no-store"
    assert "Nothing playing" in html and "Just arrived" in html and "Rumours" in html
    assert 'data-theme="dark"' in html and 'data-count="0"' in html
    assert "<script>" in html and "EventSource" in html


def test_playing_page(client, tok, app_conn):
    healthy(app_conn)
    play(app_conn, "sessions_transcode.json")
    _r, html = page(client, f"/display/now-playing?token={tok}")
    assert 'data-count="1"' in html and "Nothing playing" not in html
    assert "progress-fill" in html and "badge-warning" in html          # transcoding


def test_token_only_where_it_is_needed(client, tok, app_conn):
    healthy(app_conn)
    play(app_conn, "sessions_transcode.json")
    _r, html = page(client, f"/display/now-playing?token={tok}&size=1920x1080")
    for m in re.finditer(re.escape(tok), html):
        before = html[max(0, m.start() - 200):m.start()]
        url = before[before.rfind('"') + 1:]
        assert url.startswith(("/api/v1/events?", "/api/v1/images/", "/display/now-playing?")), url


def test_images_carry_the_token_and_a_size(client, tok, app_conn):
    healthy(app_conn)
    play(app_conn, "sessions_transcode.json")
    _r, html = page(client, f"/display/now-playing?token={tok}&size=320x240")
    srcs = re.findall(r'src="(/api/v1/images/[^"]+)"', html)
    assert srcs and all(f"token={tok}" in s and "w=160" in s for s in srcs)


def test_nothing_at_all(client, tok, app_conn):
    healthy(app_conn)
    _r, html = page(client, f"/display/now-playing?token={tok}")
    assert "Streams show here as soon as they start." in html and "Just arrived" not in html


def test_two_streams(client, tok, app_conn):
    healthy(app_conn)
    play(app_conn, "sessions_transcode.json", "s1")
    play(app_conn, "sessions_direct_play.json", "s2", user_id=1002, name="Sarah")
    _r, html = page(client, f"/display/now-playing?token={tok}")
    assert 'data-count="2"' in html and html.count("progress-fill") == 2 and "badge-accent" in html


def test_static_has_no_script(client, tok):
    _r, html = page(client, f"/display/now-playing?token={tok}&static=1")
    assert "<script" not in html and "Now playing" in html


@pytest.mark.parametrize("q,expect", [
    ("theme=light", 'data-theme="light"'),
    ("theme=sepia", 'data-theme="dark"'),
    ("accent=teal", 'class="accent-teal"'),
    ("scale=1.5", "font-size: 24px"),
    ("scale=99", "font-size: 48px"),
    ("scale=abc", "font-size: 16px"),
])
def test_options(client, tok, q, expect):
    _r, html = page(client, f"/display/now-playing?token={tok}&{q}")
    assert expect in html


def test_unknown_accent_is_ignored(client, tok):
    _r, html = page(client, f"/display/now-playing?token={tok}&accent=hotpink")
    assert "accent-hotpink" not in html and 'class="accent-' not in html.split("<head>")[0]


def test_fragment_is_just_the_body_and_keeps_options(client, tok):
    _r, full = page(client, f"/display/now-playing?token={tok}&theme=light&size=800x480")
    frag_url = re.search(r'data-fragment="([^"]+)"', full).group(1).replace("&amp;", "&")
    assert "fragment=1" in frag_url and "theme=light" in frag_url and "size=800x480" in frag_url
    _r, frag = page(client, frag_url)
    assert "<html" not in frag and "data-now-playing" in frag


def test_events_url_is_on_the_page(client, tok):
    _r, html = page(client, f"/display/now-playing?token={tok}")
    assert f'data-events="/api/v1/events?token={tok}"' in html
    names = re.search(r'data-event-names="([^"]+)"', html).group(1).split()
    assert "playback.start" in names and "arrival" in names and "pin.week" not in names


def test_admin_can_preview_without_a_token(logged_in):
    r = logged_in.get("/display/now-playing")
    assert r.status_code == 200 and 'data-events="/api/v1/events"' in r.get_data(as_text=True)


def test_settings_links_the_preview(logged_in):
    html = logged_in.get("/settings").get_data(as_text=True)
    assert "data-display-pages" in html and "/display/now-playing" in html
