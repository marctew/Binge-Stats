"""Stream limits, sharing detection and alerts (SPEC.md section 10)."""
import copy
import json

import pytest

from bingestats import db
from bingestats.services import alerts, geo
from bingestats.worker import guard
from bingestats.worker.poller import Poller
from tests.conftest import load_fixture

GEO = {
    "81.2.69.160": {"city": {"names": {"en": "London"}}, "country": {"iso_code": "GB", "names": {"en": "United Kingdom"}},
                    "location": {"latitude": 51.5, "longitude": -0.12}},
    "81.2.69.161": {"city": {"names": {"en": "Leeds"}}, "country": {"iso_code": "GB", "names": {"en": "United Kingdom"}},
                    "location": {"latitude": 53.8, "longitude": -1.5}},
    "88.12.34.56": {"city": {"names": {"en": "Malaga"}}, "country": {"iso_code": "ES", "names": {"en": "Spain"}},
                    "location": {"latitude": 36.7, "longitude": -4.4}},
}


class FakeReader:
    def get(self, ip):
        return GEO.get(ip)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_geo(tmp_path, monkeypatch):
    import maxminddb
    geo._reader_cache.clear()
    p = geo.db_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    monkeypatch.setattr(maxminddb, "open_database", lambda path: FakeReader())
    yield
    geo._reader_cache.clear()


@pytest.fixture
def events():
    return []


@pytest.fixture
def poller(conn, plex, clock, events, tmp_path):
    return Poller(conn, plex, clock=clock, on_event=lambda t, c: events.append((t, c)), data_dir=tmp_path)


def stream(n, user=1001, ip="192.168.1.50", local=True, transcode=False):
    """A session for `user`, the n-th one; WAN streams get `ip` as their public address."""
    base = load_fixture("sessions_transcode.json" if transcode else "sessions_direct_play.json")
    m = copy.deepcopy(base["MediaContainer"]["Metadata"][0])
    m["sessionKey"] = str(100 + n)
    m["Session"] = {**m["Session"], "id": f"sess{n}", "location": "lan" if local else "wan"}
    m["User"] = {**m["User"], "id": str(user), "title": f"user{user}"}
    m["Player"] = {**m["Player"], "address": ip, "local": local, "machineIdentifier": f"machine{n}"}
    m["Player"].pop("remotePublicAddress", None)
    return m


def show(plex, *metas):
    plex.sessions_data = {"MediaContainer": {"size": len(metas), "Metadata": list(metas)}}


def kinds(events):
    return [t for t, _c in events if t in ("stream_limit", "transcode_limit", "concurrent_ips", "new_country")]


def set_limits(conn, user=1001, streams=None, transcodes=None, action="notify", message=None):
    conn.execute("UPDATE users SET max_streams = ?, max_transcodes = ?, limit_action = ?, limit_message = ? WHERE id = ?",
                 (streams, transcodes, action, message, user))


def first_poll(poller, plex, clock, *metas):
    show(plex, *metas)
    clock.advance(5)
    return poller.poll_once()


# -- stream limit ------------------------------------------------------------------------

def test_stream_limit_notify_records_alert_without_stopping(conn, poller, plex, clock, events):
    first_poll(poller, plex, clock, stream(1))
    set_limits(conn, streams=1)
    r = first_poll(poller, plex, clock, stream(1), stream(2))
    assert kinds(events) == ["stream_limit"] and r.stopped == [] and plex.terminated == []
    [a] = alerts.list_alerts(conn)
    assert a["kind"] == "stream_limit" and a["user_id"] == 1001
    assert a["detail"]["count"] == 2 and a["detail"]["limit"] == 1 and a["detail"]["stopped"] is None
    assert "2 streams at once (limit 1)" in a["text"]


def test_stream_limit_stop_terminates_newest_with_message(conn, poller, plex, clock, events):
    first_poll(poller, plex, clock, stream(1))
    set_limits(conn, streams=1, action="stop", message="One at a time please")
    r = first_poll(poller, plex, clock, stream(1), stream(2))
    assert plex.terminated == [("sess2", "One at a time please")] and r.stopped == ["sess2"]
    [a] = alerts.list_alerts(conn)
    assert a["detail"]["stopped"] == "sess2" and a["text"].endswith("The newest was stopped.")
    [(_t, ctx)] = [e for e in events if e[0] == "stream_limit"]
    assert ctx["detail"]["stopped"] == "sess2" and ctx["session"]["session_id"] == "sess2"


def test_stop_uses_default_message(conn, poller, plex, clock):
    first_poll(poller, plex, clock, stream(1))
    set_limits(conn, streams=1, action="stop")
    first_poll(poller, plex, clock, stream(1), stream(2))
    assert plex.terminated == [("sess2", guard.DEFAULT_STOP_MESSAGE)]


def test_under_limit_and_other_users_dont_count(conn, poller, plex, clock, events):
    first_poll(poller, plex, clock, stream(1))
    set_limits(conn, streams=2)
    first_poll(poller, plex, clock, stream(1), stream(2), stream(3, user=2002))
    assert kinds(events) == [] and not alerts.list_alerts(conn)


def test_stream_in_grace_doesnt_count(conn, poller, plex, clock, events):
    """Stopping on the TV and starting on the phone within the grace polls is one stream, not two."""
    first_poll(poller, plex, clock, stream(1))
    set_limits(conn, streams=1, action="stop")
    first_poll(poller, plex, clock, stream(2))
    assert kinds(events) == [] and plex.terminated == []


def test_no_limit_set_means_no_checks(conn, poller, plex, clock, events):
    first_poll(poller, plex, clock, stream(1), stream(2), stream(3))
    assert kinds(events) == []


# -- transcode limit ---------------------------------------------------------------------

def test_transcode_limit_on_start(conn, poller, plex, clock, events):
    first_poll(poller, plex, clock, stream(1, transcode=True))
    set_limits(conn, transcodes=1, action="stop")
    first_poll(poller, plex, clock, stream(1, transcode=True), stream(2), stream(3, transcode=True))
    assert kinds(events) == ["transcode_limit"]
    assert plex.terminated == [("sess3", guard.DEFAULT_STOP_MESSAGE)]
    [a] = alerts.list_alerts(conn)
    assert a["detail"]["count"] == 2 and "transcodes" in a["text"]


def test_transcode_limit_when_stream_switches_to_transcode(conn, poller, plex, clock, events):
    first_poll(poller, plex, clock, stream(1, transcode=True), stream(2))
    set_limits(conn, transcodes=1)
    first_poll(poller, plex, clock, stream(1, transcode=True), stream(2, transcode=True))
    assert kinds(events) == ["transcode_limit"]
    assert [t for t, _c in events].count("transcode_start") == 2   # first poll's sess1, then sess2 switching


# -- sharing -----------------------------------------------------------------------------

def test_concurrent_public_ips_flagged_once_an_hour(conn, poller, plex, clock, events):
    a, b = stream(1, ip="81.2.69.160", local=False), stream(2, ip="88.12.34.56", local=False)
    first_poll(poller, plex, clock, a)
    first_poll(poller, plex, clock, a, b)
    assert kinds(events) == ["concurrent_ips"] and plex.terminated == []
    [al] = alerts.list_alerts(conn)
    assert al["detail"]["ips"] == ["81.2.69.160", "88.12.34.56"]
    assert "London, United Kingdom and Malaga, Spain" in al["text"]

    events.clear()
    first_poll(poller, plex, clock, a, b, stream(3, ip="81.2.69.161", local=False))
    assert "concurrent_ips" not in kinds(events)            # within the hour
    clock.advance(guard.REALERT_S)
    first_poll(poller, plex, clock, a, b, stream(4, ip="81.2.69.161", local=False))
    assert "concurrent_ips" in kinds(events)


def test_lan_and_same_public_ip_are_not_sharing(conn, poller, plex, clock, events):
    first_poll(poller, plex, clock, stream(1), stream(2, ip="81.2.69.160", local=False),
               stream(3, ip="81.2.69.160", local=False), stream(4, ip="100.70.18.103", local=False))
    assert kinds(events) == []


def test_new_country_needs_history_and_fires_once(conn, poller, plex, clock, events, tmp_path):
    first_poll(poller, plex, clock, stream(1, ip="88.12.34.56", local=False))
    assert kinds(events) == []                              # no located history yet: nothing to compare with
    conn.execute("DELETE FROM active_sessions")
    conn.execute("DELETE FROM alerts")
    conn.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, ip_address, "
                 "is_local) VALUES (1, 1001, 1700000000, 1700000060, 60, 'movie', '81.2.69.160', 0)")
    geo.lookup(conn, tmp_path, "81.2.69.160")
    events.clear()
    first_poll(poller, plex, clock, stream(2, ip="88.12.34.56", local=False))
    assert kinds(events) == ["new_country"]
    [a] = alerts.list_alerts(conn)
    assert a["detail"]["country_code"] == "ES" and a["detail"]["usual"] == ["GB"]
    assert a["text"] == "user1001 played from Malaga, Spain for the first time."

    events.clear()
    conn.execute("DELETE FROM active_sessions")
    first_poll(poller, plex, clock, stream(3, ip="88.12.34.56", local=False))
    assert kinds(events) == []                              # already flagged for Spain


def test_known_country_is_quiet(conn, poller, plex, clock, events, tmp_path):
    conn.execute("INSERT INTO users (id, username, share_salt) VALUES (1001, 'alice', 's')")
    conn.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, ip_address, "
                 "is_local) VALUES (1, 1001, 1700000000, 1700000060, 60, 'movie', '81.2.69.160', 0)")
    geo.lookup(conn, tmp_path, "81.2.69.160")
    first_poll(poller, plex, clock, stream(1, ip="81.2.69.161", local=False))
    assert kinds(events) == []


def test_limit_events_render_through_default_rules(conn, poller, plex, clock, events):
    from bingestats.services import notify
    first_poll(poller, plex, clock, stream(1))
    set_limits(conn, streams=1, action="stop")
    first_poll(poller, plex, clock, stream(1), stream(2))
    [(_t, raw)] = [e for e in events if e[0] == "stream_limit"]
    ctx = notify.build_context(conn, "stream_limit", raw)
    rule = next(r for r in notify.list_rules(conn) if r["trigger"] == "stream_limit")
    assert ctx["stopped"] is True and ctx["count"] == 2
    assert "user1001" in notify.render(rule["title_template"], ctx, 250) + notify.render(rule["body_template"], ctx, 2000)


# -- alerts service ----------------------------------------------------------------------

def _alert(conn, kind="stream_limit", user=None, at=1, detail=None):
    conn.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (?, ?, ?, ?)",
                 (at, kind, user, json.dumps(detail or {"count": 3, "limit": 2})))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_acknowledge_one_and_all(conn):
    a, b, c = _alert(conn, at=1), _alert(conn, "server_down", at=2), _alert(conn, "server_up", at=3,
                                                                              detail={"down_for_s": 4000})
    assert alerts.unacked_count(conn) == 3
    assert [x["id"] for x in alerts.list_alerts(conn)] == [c, b, a]
    assert alerts.list_alerts(conn)[0]["text"] == "Plex came back after 1h 06m."
    assert alerts.acknowledge(conn, b) == 1
    assert alerts.unacked_count(conn) == 2 and len(alerts.list_alerts(conn, include_acked=True)) == 3
    assert alerts.acknowledge(conn) == 2 and alerts.unacked_count(conn) == 0
    assert alerts.acknowledge(conn, 999) == 0


# -- migration ---------------------------------------------------------------------------

def test_migration_3_upgrades_v2_database(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "v2.db")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:2])
    monkeypatch.setattr(db, "LATEST", 2)
    assert db.migrate(c) == 2
    c.execute("INSERT INTO alerts (created_at, kind, user_id, detail) VALUES (1, 'server_down', NULL, '{}')")
    monkeypatch.undo()
    assert db.migrate(c) == db.LATEST
    idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"alerts_user", "alerts_open"} <= idx
    triggers = {r[0] for r in c.execute("SELECT trigger FROM notify_rules")}
    assert {"stream_limit", "concurrent_ips"} <= triggers
    assert c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
