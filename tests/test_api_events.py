"""The outside API standard, stage 4 (docs/specs/api-standard.md): events from the worker through the
events table, the /api/v1/events stream, and signed webhooks out."""
import hashlib
import hmac
import json
import signal
import threading
from pathlib import Path

import pytest

from bingestats.services import api_tokens, events, notify, webhooks
from bingestats import du_api
from tests.test_api_standard import bearer

T0 = 1_800_000_000


@pytest.fixture
def tok(app_conn):
    return api_tokens.create(app_conn, "Vital Signs desk", "read")


def ev(conn, type_="playback.start", title="Marc started Slow Horses", now=T0, **item):
    return events.publish(conn, type_, title, {"id": "x", "title": "Slow Horses", **item}, now)


# -- mapping ---------------------------------------------------------------------------------------

def test_every_trigger_is_an_event():
    assert set(notify.TRIGGERS) == set(events.TRIGGER_EVENTS)
    assert "download.finished" in events.TYPES and len(events.TYPES) == len(set(events.TYPES))


@pytest.mark.parametrize("trigger", sorted(notify.TRIGGERS))
def test_every_trigger_makes_a_clean_item(trigger):
    title, item = events.from_context(trigger, notify.sample_context(trigger))
    assert isinstance(title, str) and title
    assert item["id"] and item["title"] and item["at"].endswith("Z")
    assert None not in item.values() and None not in (item.get("meta") or {}).values()


def test_session_event_reads_well():
    ctx = notify.sample_context("play_start")
    ctx["item"]["rating_key"] = 58213
    title, item = events.from_context("play_start", ctx)
    assert title.split(" started ")[1] == item["title"]
    assert item["image"] == "/api/v1/images/poster/58213" and item["meta"]["rating_key"] == 58213


def test_meta_lists_the_event_types(app, tok):
    m = app.test_client().get("/api/v1/meta", headers=bearer(tok)).get_json()
    assert m["events"] == events.TYPES


# -- the table ----------------------------------------------------------------------------------------

def test_publish_and_read_back(conn):
    a = ev(conn)
    b = ev(conn, "arrival", "Just arrived: Rumours", now=T0 + 5)
    got = events.after(conn, 0)
    assert [e["id"] for e in got] == [a, b] and events.after(conn, a)[0]["type"] == "arrival"
    assert got[0] == {"id": a, "type": "playback.start", "at": "2027-01-15T08:00:00Z", "title": "Marc started Slow Horses",
                      "item": {"id": "x", "title": "Slow Horses"}}
    assert events.last_id(conn) == b


def test_prune_keeps_an_hour_and_at_least_200(conn):
    for i in range(250):
        ev(conn, now=T0 + i)
    assert events.prune(conn, now=T0 + 4000) == 50          # all old, but the last 200 stay
    ev(conn, now=T0 + 4000)
    assert events.prune(conn, now=T0 + 4001) == 1
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 200


def test_worker_publishes_triggers(conn):
    eid = events.publish_trigger(conn, "server_down", notify.sample_context("server_down"))
    assert events.get(conn, eid)["title"] == "Plex is down"
    assert events.publish_trigger(conn, "not_a_trigger", {}) is None


# -- the stream generator --------------------------------------------------------------------------------

class Fake:
    def __init__(self):
        self.t = 0.0
        self.rows: list[dict] = []
        self.stopping = threading.Event()

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def after(self, last):
        return [r for r in self.rows if r["id"] > last]


def row(i, type_="arrival"):
    return {"id": i, "type": type_, "at": "2027-01-15T08:00:00Z", "title": f"t{i}", "item": {"id": str(i), "title": "x"}}


def test_stream_replays_then_follows_then_pings():
    f = Fake()
    f.rows = [row(1), row(2), row(3)]
    g = du_api.event_stream(f.after, 1, stopping=f.stopping, clock=f.clock, sleep=f.sleep, max_s=60)
    assert next(g) == "retry: 5000\n\n"
    assert next(g).startswith("event: arrival\nid: 2\ndata: {")
    assert next(g).startswith("event: arrival\nid: 3\n")
    f.rows.append(row(4, "playback.start"))
    chunk = next(g)
    assert chunk.startswith("event: playback.start\nid: 4\n") and f.t == 1.0
    data = json.loads(chunk.split("data: ", 1)[1])
    assert data["id"] == 4 and data["item"]["title"] == "x"
    assert next(g) == ": ping\n\n" and f.t == 25.0
    assert next(g) == ": ping\n\n" and f.t == 50.0
    assert list(g) == []                                     # max_s reached
    assert f.t == 60.0


def test_stream_ends_when_stopping():
    f = Fake()
    g = du_api.event_stream(f.after, 0, stopping=f.stopping, clock=f.clock, sleep=f.sleep)
    next(g)
    f.stopping.set()
    assert list(g) == []


def test_stream_slots():
    slots = du_api.StreamSlots(2)
    a, b = slots.take(), slots.take()
    assert a and b and slots.take() is None
    a()
    a()                                                       # releasing twice is harmless
    assert slots.take() is not None and slots.take() is None


def test_sigterm_ends_streams_and_chains(app):
    core = app.extensions["du_api"]
    called = []
    old = signal.signal(signal.SIGTERM, lambda s, f: called.append(s))
    try:
        du_api.stop_streams_on_sigterm(core)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert core.stopping.is_set() and called == [signal.SIGTERM]
    finally:
        signal.signal(signal.SIGTERM, old)


# -- the route ---------------------------------------------------------------------------------------

def read_stream(app, url, headers, n):
    """The first n chunks, then stop the stream."""
    core = app.extensions["du_api"]
    r = app.test_client().get(url, headers=headers, buffered=False)
    assert r.status_code == 200, r.get_data()
    out, it = [], iter(r.response)
    for _ in range(n):
        c = next(it)
        out.append(c.decode() if isinstance(c, bytes) else c)
    core.stopping.set()
    list(it)
    r.close()
    core.stopping.clear()
    return r, out


def test_events_route_replays_after_last_event_id(app, app_conn, tok):
    a, b = ev(app_conn), ev(app_conn, "arrival", "Just arrived: Rumours")
    r, chunks = read_stream(app, "/api/v1/events", {**bearer(tok), "Last-Event-ID": str(a)}, 2)
    assert r.mimetype == "text/event-stream" and r.headers["Cache-Control"] == "no-cache"
    assert chunks[0].startswith("retry:") and chunks[1].startswith(f"event: arrival\nid: {b}\n")


def test_events_route_url_token_and_query_last_id(app, app_conn, tok):
    a = ev(app_conn)
    _r, chunks = read_stream(app, f"/api/v1/events?token={tok}&last_event_id=0", {}, 2)
    assert f"id: {a}\n" in chunks[1]


def test_events_route_without_last_id_replays_nothing(app, app_conn, tok):
    ev(app_conn)
    core = app.extensions["du_api"]
    core.stopping.set()                       # end at once: only the retry line comes out
    r = app.test_client().get("/api/v1/events", headers=bearer(tok))
    body = r.get_data(as_text=True)          # the test client reads lazily: before clearing the flag
    core.stopping.clear()
    assert body == "retry: 5000\n\n"


def test_events_route_cap_and_release(app, tok):
    core = app.extensions["du_api"]
    core.streams = du_api.StreamSlots(1)
    hold = core.streams.take()
    r = app.test_client().get("/api/v1/events", headers=bearer(tok))
    assert r.status_code == 503 and r.headers["Retry-After"] == "30" and r.get_json()["code"] == "busy"
    hold()
    read_stream(app, "/api/v1/events", bearer(tok), 1)
    assert core.streams.take() is not None                     # the stream gave its slot back


def test_events_route_bad_last_id(app, tok):
    r = app.test_client().get("/api/v1/events", headers={**bearer(tok), "Last-Event-ID": "abc"})
    assert r.status_code == 400
    assert app.extensions["du_api"].streams.take() is not None


def test_events_need_a_token_and_skip_the_rate_limit(app, tok):
    assert app.test_client().get("/api/v1/events").status_code == 401
    assert "api.std_events" in app.extensions["du_api"].unmetered_endpoints


def test_gunicorn_has_room_for_streams():
    sh = (Path(__file__).parent.parent / "docker" / "entrypoint.sh").read_text()
    assert "--threads 12" in sh


# -- webhooks ---------------------------------------------------------------------------------------

class Poster:
    def __init__(self, fail=0):
        self.fail, self.sent = fail, []

    def __call__(self, url, body, headers):
        self.sent.append((url, body, headers))
        if self.fail:
            self.fail -= 1
            raise RuntimeError("HTTP 500")


def test_create_validates_and_hides_the_url(conn):
    with pytest.raises(ValueError):
        webhooks.create(conn, "HA", "ftp://x")
    with pytest.raises(ValueError):
        webhooks.create(conn, "", "http://ha.lan/api/webhook/x")
    secret = webhooks.create(conn, "Home Assistant", "http://ha.lan:8123/api/webhook/SECRETID", ["arrival", "bogus"])
    [w] = webhooks.list_webhooks(conn)
    assert len(secret) > 30 and "url" not in w and "secret" not in w
    assert w["masked_url"] == "http://ha.lan:8123/•••" and w["event_types"] == ["arrival"]


def test_signature_known_vector():
    body = b'{"id":1}'
    want = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert webhooks.sign(body, "s3cret") == want


def test_delivery_signed_and_filtered(conn):
    s_all = webhooks.create(conn, "All", "http://n8n.lan/webhook/a")
    webhooks.create(conn, "Arrivals", "http://ha.lan/api/webhook/b", ["arrival"])
    ev(conn, now=T0)
    ev(conn, "arrival", "Just arrived", now=T0)
    post = Poster()
    assert webhooks.deliver_due(conn, now=T0, post=post) == 3
    urls = [u for u, _b, _h in post.sent]
    assert urls.count("http://n8n.lan/webhook/a") == 2 and urls.count("http://ha.lan/api/webhook/b") == 1
    url, body, headers = post.sent[0]
    assert headers["X-Signature"] == webhooks.sign(body, s_all) and headers["Content-Type"] == "application/json"
    assert json.loads(body)["type"] == headers["X-Event-Type"] == "playback.start"
    assert webhooks.deliver_due(conn, now=T0 + 1, post=post) == 0
    assert all(w["last_ok_at"] == T0 for w in webhooks.list_webhooks(conn))


def test_retries_after_1_3_6_minutes_then_gives_up(conn):
    webhooks.create(conn, "HA", "http://ha.lan/api/webhook/x")
    ev(conn, now=T0)
    post = Poster(fail=99)
    tried_at = []
    t = T0
    while t < T0 + 3600:
        if webhooks.deliver_due(conn, now=t, post=post):
            tried_at.append(t - T0)
        t += 30
    assert tried_at == [0, 60, 240, 600]
    d = conn.execute("SELECT attempts, done_at, error FROM webhook_deliveries").fetchone()
    assert d["attempts"] == 4 and d["done_at"] == T0 + 600 and d["error"] == "HTTP 500"
    [w] = webhooks.list_webhooks(conn)
    assert w["last_error"] == "HTTP 500" and w["last_ok_at"] is None and w["pending"] == 0


def test_recovers_on_retry(conn):
    webhooks.create(conn, "HA", "http://ha.lan/api/webhook/x")
    ev(conn, now=T0)
    post = Poster(fail=1)
    webhooks.deliver_due(conn, now=T0, post=post)
    webhooks.deliver_due(conn, now=T0 + 60, post=post)
    assert len(post.sent) == 2 and webhooks.list_webhooks(conn)[0]["last_ok_at"] == T0 + 60


def test_disabled_or_deleted_webhooks_get_nothing(conn):
    webhooks.create(conn, "HA", "http://ha.lan/api/webhook/x")
    conn.execute("UPDATE webhooks SET enabled = 0")
    ev(conn)
    assert conn.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 0
    conn.execute("UPDATE webhooks SET enabled = 1")
    ev(conn)
    webhooks.delete(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 0


def test_expired_event_is_dropped(conn):
    webhooks.create(conn, "HA", "http://ha.lan/api/webhook/x")
    eid = ev(conn, now=T0)
    conn.execute("DELETE FROM events WHERE id = ?", (eid,))
    post = Poster()
    webhooks.deliver_due(conn, now=T0, post=post)
    assert post.sent == [] and conn.execute("SELECT error FROM webhook_deliveries").fetchone()[0] == "event expired"


def test_new_secret_and_prune(conn):
    old = webhooks.create(conn, "HA", "http://ha.lan/api/webhook/x")
    assert webhooks.new_secret(conn, 1) not in (None, old) and webhooks.new_secret(conn, 99) is None
    ev(conn, now=T0)
    webhooks.deliver_due(conn, now=T0, post=Poster())
    assert webhooks.prune(conn, now=T0 + 8 * 86400) == 1


# -- download.finished ------------------------------------------------------------------------------

def test_finished_downloads_are_published_once_and_not_at_startup(conn):
    from bingestats.worker.downloads import DownloadWatcher
    w = DownloadWatcher(Path("unused.db"), threading.Event())
    old = {"nzo_id": "SAB_old", "status": "Completed", "name": "Old.S01E01", "mb": 900.0, "completed_at": T0 - 600}
    w.st["known"] = {"SAB_new": [{"app": "sonarr", "title": "Slow Horses", "season": 5, "episode": 3, "tvdb_id": 1}],
                     "SAB_old": [{"app": "sonarr", "title": "Old", "season": 1, "episode": 1}]}
    w.publish_finished(conn, {"recent": [old], "pp": []}, T0)                      # startup: nothing announced
    assert events.last_id(conn) == 0
    new = {"nzo_id": "SAB_new", "status": "Completed", "name": "x", "mb": 1200.0, "completed_at": T0}
    other = {"nzo_id": "SAB_misc", "status": "Completed", "name": "linux.iso", "mb": 4000.0, "completed_at": T0}
    failed = {"nzo_id": "SAB_fail", "status": "Failed", "name": "y", "mb": 1.0, "completed_at": T0}
    w.publish_finished(conn, {"recent": [new, other, failed, old], "pp": []}, T0 + 5)
    w.publish_finished(conn, {"recent": [new, other, failed, old], "pp": []}, T0 + 10)
    [e] = events.after(conn, 0)
    assert e["type"] == "download.finished" and e["title"] == "Downloaded Slow Horses S05E03"
    assert e["item"]["id"] == "sab:SAB_new" and e["item"]["meta"]["tvdb_id"] == 1


# -- Settings ------------------------------------------------------------------------------------------

def test_settings_webhooks_card(logged_in, app_conn):
    html = logged_in.get("/settings").get_data(as_text=True)
    assert 'id="webhooks"' in html and "No webhooks yet." in html and 'value="download.finished"' in html
    r = logged_in.post("/settings/webhooks", {"name": "Home Assistant", "url": "http://ha.lan:8123/api/webhook/SECRETID",
                                              "event_types": "arrival"})
    html = r.get_data(as_text=True)
    secret = app_conn.execute("SELECT secret FROM webhooks").fetchone()[0]
    assert r.status_code == 200 and secret in html and "SECRETID" not in html
    assert "http://ha.lan:8123/•••" in html
    html = logged_in.get("/settings").get_data(as_text=True)
    assert secret not in html and "SECRETID" not in html and "nothing sent yet" in html


def test_settings_webhook_bad_url(logged_in, app_conn):
    r = logged_in.post("/settings/webhooks", {"name": "x", "url": "not a url"})
    assert r.status_code == 302 and app_conn.execute("SELECT COUNT(*) FROM webhooks").fetchone()[0] == 0


def test_settings_webhook_new_secret_and_delete(logged_in, app_conn):
    old = webhooks.create(app_conn, "HA", "http://ha.lan/api/webhook/x")
    r = logged_in.post("/settings/webhooks/1/secret")
    new = app_conn.execute("SELECT secret FROM webhooks").fetchone()[0]
    assert new != old and new in r.get_data(as_text=True)
    assert logged_in.post("/settings/webhooks/99/secret").status_code == 404
    assert logged_in.post("/settings/webhooks/1/delete").status_code == 302
    assert webhooks.list_webhooks(app_conn) == []
