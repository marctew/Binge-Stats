"""Static files and caching (Design Uniformity APP-STANDARD 12): pages are no-store, static files no-cache, every
static link carries ?v=<mtime>, and responses that set their own caching (the /img/ proxy, display pages) keep it."""
import re
import shutil
from pathlib import Path

import pytest
import responses

from bingestats.services import api_tokens
from tests.test_api_standard import healthy

LINK = re.compile(r'(?:href|src)="(/static/[^"]*)"')


@pytest.fixture
def built_static(app, tmp_path):
    """The static folder as the Docker image has it. css/app.css and vendor/*.js are built or fetched at build time
    and gitignored, so a checkout lacks them (and du_flask leaves a missing file's link plain): copy the folder and
    add stand-ins for whatever built file the pages link."""
    mirror = tmp_path / "static"
    shutil.copytree(app.static_folder, mirror)
    app.static_folder = str(mirror)

    def add_missing(html):
        for link in LINK.findall(html):
            rel = link.split("?")[0].removeprefix("/static/")
            if not (mirror / rel).exists():
                assert rel == "css/app.css" or rel.startswith("vendor/"), f"{link} links a file that isn't there"
                (mirror / rel).parent.mkdir(parents=True, exist_ok=True)
                (mirror / rel).write_text("/* built */")
    return add_missing


def _versioned(html):
    links = LINK.findall(html)
    assert links, "the page links no static files"
    for link in links:
        assert re.search(r"\?v=[\w.]+$", link) and link.count("?") == 1, link
    return links


def test_static_links_carry_a_version(logged_in, app, built_static):
    for url in ("/", "/graphs", "/login"):
        client = logged_in if url != "/login" else app.test_client()
        built_static(client.get(url).get_data(as_text=True))
        links = _versioned(client.get(url).get_data(as_text=True))
        # Everything but the two PWA links (v=app_version, PWA-ICONS.md) is versioned by the file's mtime.
        for link in links:
            if "apple-touch-icon" not in link and "manifest.json" not in link:
                assert re.search(r"\?v=\d+$", link), link
    assert any("/static/css/app.css?v=" in link for link in links)


def test_display_page_css_is_versioned_and_still_no_store(app, app_conn, built_static):
    tok = api_tokens.create(app_conn, "Kitchen kiosk", "read")
    healthy(app_conn)
    app_conn.commit()
    client = app.test_client()
    built_static(client.get(f"/display/now-playing?token={tok}").get_data(as_text=True))
    r = client.get(f"/display/now-playing?token={tok}")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert re.search(r'href="/static/css/app\.css\?v=\d+"', r.get_data(as_text=True))


def test_pages_are_no_store(logged_in, app):
    for r in (logged_in.get("/"), app.test_client().get("/login")):
        assert r.status_code == 200 and r.mimetype == "text/html"
        assert r.headers["Cache-Control"] == "no-store"


def test_static_files_are_no_cache(app):
    r = app.test_client().get("/static/js/app.js")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-cache" and "Expires" not in r.headers
    assert r.headers.get("ETag")                                   # so a revalidation is a 304, not a download
    again = app.test_client().get("/static/js/app.js", headers={"If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304


@responses.activate
def test_image_proxy_keeps_its_week_long_cache(logged_in):
    responses.get("http://plex.test:32400/photo/:/transcode", body=b"\xff\xd8jpeg", content_type="image/jpeg")
    for _ in range(2):                                             # fetched from Plex, then from the disk cache
        r = logged_in.get("/img/plex?path=/library/metadata/1/thumb/2")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == f"private, max-age={7 * 24 * 3600}"


def test_more_in_the_bottom_nav(logged_in):
    """Design Uniformity 6.2, in Binge's own Alpine shell: the `more` glyph, rows that open upwards, accent when the
    current page is inside it, and closing on a tap outside or Escape."""
    html = logged_in.get("/").get_data(as_text=True)
    more = html[html.index('class="nav-more'):]
    more = more[:more.index("</nav>")]
    assert '@click.outside="more = false"' in more and '@keydown.escape.window="more = false"' in more
    assert '<circle cx="12" cy="5"' in more                        # the `more` glyph
    assert "bottom-full" in more and 'class="menu-item' in more
    assert "'!text-accent': more" in more and "active text-accent" not in more
    settings_page = logged_in.get("/settings").get_data(as_text=True)
    more = settings_page[settings_page.index('class="nav-more'):]
    assert 'class="flex flex-1 flex-col items-center gap-1 py-2.5 text-[11px] font-medium' in more
    assert "active text-accent" in more[:more.index("More</button>")]   # Settings lives inside More
