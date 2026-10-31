"""Location map basemap: OpenStreetMap by default, or a tile URL from Settings."""
import json
import re

from bingestats.services import settings


def _seed(c):
    c.execute("INSERT INTO users (id, username, share_salt) VALUES (5, 'sam', 's')")
    c.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, title, ip_address) "
              "VALUES (1, 5, 1700000000, 1700000600, 600, 'movie', 'Film', '81.2.69.160')")
    c.execute("INSERT INTO ip_geo (ip, is_private, city, country_code, country, lat, lon, looked_up_at) "
              "VALUES ('81.2.69.160', 0, 'London', 'GB', 'United Kingdom', 51.5, -0.12, 1)")


def _tiles(body: str) -> dict:
    m = re.search(r"data-tiles='([^']+)'", body)
    assert m, "map has no tile config"
    return json.loads(m.group(1).replace("&#34;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))


def test_osm_by_default_and_custom_url(logged_in, app_conn):
    _seed(app_conn)
    t = _tiles(logged_in.get("/users/5").data.decode())
    assert t["url"] == "https://tile.openstreetmap.org/{z}/{x}/{y}.png" and t["osm"] is True
    assert "carto" not in logged_in.get("/users/5").data.decode().lower()

    r = logged_in.post("/settings", {"ui.timezone": "Europe/London", "poll.interval_s": "5", "ingest.min_play_s": "30",
                                     "ingest.grace_polls": "3", "ingest.watched_pct.movie": "85",
                                     "ingest.watched_pct.episode": "85", "ingest.watched_pct.track": "50",
                                     "ingest.group_resume_h": "24", "library.sync_h": "6", "binge.gap_min": "30",
                                     "binge.min_episodes": "3", "abandoned.days": "60", "library.stale_months": "12",
                                     "map.tile_url": "http://tiles.example/{z}/{x}/{y}.png"}, follow_redirects=True)
    assert b"needs https://" in r.data and settings.get(app_conn, "map.tile_url") == ""
    settings.set(app_conn, "map.tile_url", "https://tiles.example/{z}/{x}/{y}.png?key=abc")
    settings.set(app_conn, "map.tile_attribution", "Example")
    t = _tiles(logged_in.get("/users/5").data.decode())
    assert t == {"url": "https://tiles.example/{z}/{x}/{y}.png?key=abc", "attribution": "Example", "osm": False}
