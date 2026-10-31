import pytest

from bingestats.services import settings


def test_defaults_and_roundtrip(conn):
    assert settings.get(conn, "poll.interval_s") == 5
    settings.set(conn, "poll.interval_s", "10")
    assert settings.get(conn, "poll.interval_s") == 10
    settings.set(conn, "poll.use_websocket", 0)
    assert settings.get(conn, "poll.use_websocket") is False


def test_bounds_enforced(conn):
    with pytest.raises(ValueError):
        settings.set(conn, "poll.interval_s", 1)


def test_unknown_key(conn):
    with pytest.raises(KeyError):
        settings.get(conn, "nope")
    with pytest.raises(KeyError):
        settings.set(conn, "nope", 1)


def test_get_many_prefix(conn):
    settings.set(conn, "ingest.min_play_s", 45)
    got = settings.get_many(conn, "ingest.")
    assert got["ingest.min_play_s"] == 45
    assert "poll.interval_s" not in got
