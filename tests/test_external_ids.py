"""The quick check for new items mustn't wipe the tvdb/tmdb/imdb ids the full sync stored (they were
being reset to NULL every 10 minutes for the newest items, which hid the pin button and broke
Sonarr/Radarr matching for anything recently added)."""
from bingestats.plex.client import PlexClient
from bingestats.worker.library_sync import ITEM_COLS, _upsert_items


def item(**kw):
    return {**{c: None for c in ITEM_COLS}, "rating_key": 120212, "type": "show", "title": "A Tale of Two Cities",
            "guid": "plex://show/t", **kw}


def ids(conn):
    return tuple(conn.execute("SELECT tvdb_id, tmdb_id, imdb_id, title FROM media_items").fetchone())


def test_a_read_without_ids_keeps_the_ones_stored(conn):
    conn.execute("INSERT INTO servers (id, machine_id, name) VALUES (1, 'm', 'Plex')")
    conn.execute("INSERT INTO libraries (id, server_id, name, type) VALUES (5, 1, 'TV', 'show')")
    _upsert_items(conn, [item(tvdb_id=468257, tmdb_id=301626, imdb_id="tt38128417")], 5, 1, 100, announce=False)
    _upsert_items(conn, [item(title="A Tale of Two Cities (2026)")], 5, 1, 200, announce=False)
    assert ids(conn) == (468257, 301626, "tt38128417", "A Tale of Two Cities (2026)")   # other fields still update
    _upsert_items(conn, [item(tvdb_id=999)], 5, 1, 300, announce=False)
    assert ids(conn)[0] == 999                                                          # a real new id still wins


def test_the_recent_check_asks_plex_for_ids(monkeypatch):
    sent = {}
    client = PlexClient("http://plex.test:32400", "tok", "client-id")
    monkeypatch.setattr(client, "_request", lambda path, params=None, **kw: sent.update(params or {}) or {})
    client.section_recent(5, 2)
    assert sent.get("includeGuids") == 1
