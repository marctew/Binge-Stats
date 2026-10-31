import gzip

import pytest
import responses

from bingestats.services import geo, jobs, settings
from bingestats.worker import jobs_runner

RECORDS = {
    "81.2.69.160": {"city": {"names": {"en": "London"}}, "country": {"iso_code": "GB", "names": {"en": "United Kingdom"}},
                    "subdivisions": [{"names": {"en": "England"}}], "location": {"latitude": 51.5, "longitude": -0.12}},
    "88.12.34.56": {"city": {"names": {"en": "Malaga"}}, "country": {"iso_code": "ES", "names": {"en": "Spain"}},
                    "location": {"latitude": 36.7, "longitude": -4.4}},
}


class FakeReader:
    def get(self, ip):
        return RECORDS.get(ip)

    def close(self):
        pass


@pytest.fixture
def fake_db(tmp_path, monkeypatch):
    import maxminddb
    p = geo.db_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    monkeypatch.setattr(maxminddb, "open_database", lambda path: FakeReader())
    return tmp_path


def add_play(conn, ip, user=1, local=0, started=1_700_000_000):
    conn.execute("INSERT OR IGNORE INTO users (id, username, share_salt) VALUES (?, 'u', 's')", (user,))
    conn.execute("INSERT INTO plays (server_id, user_id, started_at, stopped_at, play_s, media_type, ip_address, is_local) "
                 "VALUES (1, ?, ?, ?, 60, 'movie', ?, ?)", (user, started, started + 60, ip, local))


@pytest.mark.parametrize("ip, private", [("192.168.4.50", True), ("10.0.0.1", True), ("100.70.18.103", True),
                                         ("127.0.0.1", True), ("garbage", True), ("81.2.69.160", False),
                                         ("2a00:1450::1", False), ("fd00::1", True)])
def test_is_private(ip, private):
    assert geo.is_private(ip) is private


def test_fill_and_locations(conn, fake_db):
    add_play(conn, "192.168.4.50", local=1)
    add_play(conn, "81.2.69.160")
    add_play(conn, "81.2.69.160", started=1_700_100_000)
    add_play(conn, "88.12.34.56")
    add_play(conn, "100.70.18.103")             # Tailscale, remote
    add_play(conn, "51.15.0.1")              # not in the database
    r = geo.fill(conn, fake_db)
    assert r == {"looked_up": 2, "private": 2, "unknown": 1, "pending_no_db": False}
    assert geo.pending(conn) == 0
    locs = {l["label"]: l for l in geo.locations(conn)}
    assert locs["London, United Kingdom"]["plays"] == 2 and locs["London, United Kingdom"]["lat"] == 51.5
    assert "Home network" in locs and locs["Home network"]["lat"] is None
    assert "Private network or VPN" in locs and "Unknown location" in locs
    assert geo.fill(conn, fake_db)["looked_up"] == 0          # nothing stale


def test_fill_without_database_only_marks_private(conn, tmp_path):
    add_play(conn, "192.168.1.2", local=1)
    add_play(conn, "81.2.69.160")
    r = geo.fill(conn, tmp_path)
    assert r["private"] == 1 and r["pending_no_db"] is True
    assert geo.pending(conn) == 1
    assert {l["label"] for l in geo.locations(conn)} == {"Home network", "Not looked up yet"}


def test_locations_per_user(conn, fake_db):
    add_play(conn, "81.2.69.160", user=1)
    add_play(conn, "88.12.34.56", user=2)
    geo.fill(conn, fake_db)
    assert [l["label"] for l in geo.locations(conn, user_id=2)] == ["Malaga, Spain"]


@responses.activate
def test_download_falls_back_to_last_month(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "_check", lambda p: None)
    responses.get("https://download.db-ip.com/free/dbip-city-lite-2026-11.mmdb.gz", status=404)
    responses.get("https://download.db-ip.com/free/dbip-city-lite-2026-10.mmdb.gz", body=gzip.compress(b"MMDB"))
    import calendar
    r = geo.download(tmp_path, now=calendar.timegm((2026, 11, 1, 3, 0, 0)))
    assert r["month"] == "2026-10" and geo.db_path(tmp_path).read_bytes() == b"MMDB"
    assert not geo.needs_download(tmp_path)
    assert not list(geo.db_path(tmp_path).parent.glob("*.part"))


def test_geo_job_scheduled_when_ips_pending(conn):
    conn.execute("INSERT INTO servers (machine_id) VALUES ('m')")
    settings.set(conn, "plex.url", "http://p")
    settings.set(conn, "plex.token", "t")
    settings.set(conn, "library.last_sync_at", 10**10)
    settings.set(conn, "users.last_sync_at", 10**10)
    settings.set(conn, "wrapped.last_run_at", 10**10)
    add_play(conn, "81.2.69.160")
    queued = jobs_runner.schedule_due(conn, now=10**10)
    assert [jobs.get(conn, j)["kind"] for j in queued] == ["geo"]
    settings.set(conn, "geo.enabled", False)
    jobs.claim_next(conn)
    assert jobs_runner.schedule_due(conn, now=10**10) == []
