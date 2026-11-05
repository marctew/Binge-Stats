"""Pin messages: arrived (once per person per episode), air date moved, the week ahead; and the two
Home Assistant sensors per tracked person."""
import json
from datetime import datetime, timezone

import pytest

from bingestats import db
from bingestats.services import notify, pin_notify, pins, settings
from bingestats.worker import mqtt
from tests.test_pins import ALEX, NOW, OWNER, D, H, library

SHOW_RK = 10


@pytest.fixture
def lib(conn):
    library(conn)
    pins.pin(conn, OWNER, 111)
    pins.pin(conn, ALEX, 111)
    return conn


def added(*eps, key="episode:10:1:500"):
    return {"kind": "episode", "show": "Pinned Show", "season": 1, "show_rating_key": SHOW_RK, "poster": "/p",
            "key": key, "items": [{"title": f"E{e}", "season": 1, "episode": e, "year": 2026,
                                   "display_title": f"Pinned Show · S01E{e:02d}"} for e in eps]}


# -- arrived -----------------------------------------------------------------------------------

def test_one_event_per_pinner_and_never_twice(lib):
    evs = pin_notify.arrivals(lib, added(4, 5), now=NOW)
    assert [(e["pinner"]["id"], e["count"], e["headline"]) for e in evs] == [
        (OWNER, 2, "Pinned Show · Season 1 · 2 episodes"), (ALEX, 2, "Pinned Show · Season 1 · 2 episodes")]
    assert evs[0]["pinner"]["name"] == "Marc" and evs[0]["poster"] == "/p"
    assert pin_notify.arrivals(lib, added(4, 5), now=NOW) == []            # an upgrade or re-add: already told
    [one] = pin_notify.arrivals(lib, added(5, 6), now=NOW)[:1]
    assert (one["count"], one["headline"]) == (1, "Pinned Show · S01E06")  # only the new one


def test_notify_off_unpinned_and_films_get_nothing(lib):
    lib.execute("UPDATE pins SET notify = 0 WHERE user_id = ?", (ALEX,))
    assert [e["pinner"]["id"] for e in pin_notify.arrivals(lib, added(7), now=NOW)] == [OWNER]
    assert pin_notify.arrivals(lib, {**added(8), "show_rating_key": 30}, now=NOW) == []   # nobody pinned it
    assert pin_notify.arrivals(lib, {"kind": "movie", "items": []}, now=NOW) == []


# -- air date moved ------------------------------------------------------------------------------

def test_moves_are_queued_once_per_pinner(lib):
    before = pin_notify.air_dates(lib)
    lib.execute("UPDATE sonarr_episodes SET air_at = air_at + 7 * 86400 WHERE series_id = 1 AND episode = 5")  # a week later
    lib.execute("UPDATE sonarr_episodes SET air_at = air_at + 600 WHERE series_id = 1 AND episode = 4")       # 10 minutes: noise
    lib.execute("UPDATE sonarr_episodes SET air_at = air_at - 86400 WHERE series_id = 1 AND episode = 9")     # long gone
    assert pin_notify.queue_moves(lib, before, now=NOW) == 2              # E5, for both pinners
    assert pin_notify.queue_moves(lib, before, now=NOW) == 0              # the same move isn't news twice
    sent = pin_notify.pending(lib, now=NOW)
    assert [(t, p["pinner"]["id"], p["code"], p["later"]) for t, p in sent] == [
        ("pin_schedule_changed", OWNER, "S01E05", True), ("pin_schedule_changed", ALEX, "S01E05", True)]
    assert sent[0][1]["new_text"].startswith("Sun 15 Nov") and sent[0][1]["old_text"].startswith("Sun 8 Nov")
    assert pin_notify.pending(lib, now=NOW) == []                          # handed over once


def test_a_sonarr_sync_queues_moves(lib):
    import responses
    from bingestats.worker import arr_sync
    from tests import test_arr as ta
    ta.connect(lib)
    lib.execute("DELETE FROM sonarr_episodes")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        ta.mock_all(rsps)                                                 # Sonarr series 1 is tvdb 111, pinned
        rsps.add(responses.GET, f"{ta.SON}/api/v3/episode", json=[ta.ep(1, 3, 1, air="2026-11-20T20:00:00Z")])
        arr_sync.run(lib, now=ta.NOW)
        assert lib.execute("SELECT COUNT(*) FROM pin_events").fetchone()[0] == 0
        rsps.replace(responses.GET, f"{ta.SON}/api/v3/episode", json=[ta.ep(1, 3, 1, air="2026-11-27T20:00:00Z")])
        result = arr_sync.run(lib, now=ta.NOW)
    assert result["synced"]["sonarr"]["moves"] == 2                       # one per pinner
    [(trigger, p), _] = pin_notify.pending(lib, now=ta.NOW)
    assert (trigger, p["show"], p["code"]) == ("pin_schedule_changed", "Nearly There", "S03E01")


# -- the week ahead ------------------------------------------------------------------------------

def test_week_due_on_monday_morning(lib):
    mon_7 = datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc).timestamp()    # London is UTC in November
    mon_8 = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc).timestamp()
    assert not pin_notify.week_due(lib, mon_7)                              # before 08:00
    assert pin_notify.week_due(lib, mon_7 - 2 * D)                         # last week's never went
    settings.set(lib, "pins.last_week_at", int(mon_7))
    assert pin_notify.week_due(lib, mon_8)
    pin_notify.week_events(lib, mon_8)
    assert not pin_notify.week_due(lib, mon_8 + 3600)
    assert pin_notify.week_due(lib, mon_8 + 7 * D)


def test_week_events(lib):
    for n in range(1, 4):   # a season drop on Tuesday
        lib.execute("INSERT INTO sonarr_episodes VALUES (1, 2, ?, 'x', ?, 0, 1)", (n, int(NOW) + 5 * D))
    evs = pin_notify.week_events(lib, NOW)
    assert [e["pinner"]["id"] for e in evs] == [OWNER, ALEX]
    days = {d["label"]: [x["text"] for x in d["episodes"]] for d in evs[0]["days"]}
    assert days["Thursday 5 November"] == ["Pinned Show S01E04 · Tonight · 14:00"]
    assert days["Tuesday 10 November"] == ["Pinned Show S02E01–E03 · 3 episodes · 12:00"]
    assert evs[0]["count"] == 5
    assert pin_notify.week_events(lib, NOW + 60) == []                    # one per person per week


# -- through the rules engine ---------------------------------------------------------------------

def test_rules_render_and_pick_the_pinner(lib):
    rules = {r["trigger"]: r for r in notify.list_rules(lib)}
    assert {"pin_arrived", "pin_schedule_changed", "pins_week"} <= set(rules)
    assert all(not rules[t]["enabled"] for t in ("pin_arrived", "pin_schedule_changed", "pins_week"))
    for t in ("pin_arrived", "pin_schedule_changed", "pins_week"):
        ctx = notify.sample_context(t)
        assert notify.render(rules[t]["body_template"], ctx, 4000)
    ch = notify.save_channel(lib, "Alex's phone", "ntfy://ntfy.example/alex")
    notify.save_rule(lib, {"name": "Alex's arrivals", "trigger": "pin_arrived", "enabled": True, "channel_ids": [ch],
                           "title_template": "{{ headline }}", "body_template": "{{ pinner.name }}",
                           "conditions": [{"field": "pinner.id", "op": "in", "value": [str(ALEX)]}]})
    n = notify.Notifier(lib, ":memory:", start_thread=False, sender=lambda *a: (True, None))
    planned = [n.handle("pin_arrived", e) for e in pin_notify.arrivals(lib, added(11), now=NOW)]
    assert [len(p) for p in planned] == [0, 1]                            # only Alex's event matches Alex's rule
    assert planned[1][0].title == "Pinned Show · S01E11" and planned[1][0].body == "Alex"


def test_migration_12(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "v11.db")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:11])
    monkeypatch.setattr(db, "LATEST", 11)
    db.migrate(c)
    monkeypatch.undo()
    assert db.migrate(c) == db.LATEST
    assert c.execute("SELECT COUNT(*) FROM pin_events").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM notify_rules WHERE trigger IN "
                     "('pin_arrived', 'pin_schedule_changed', 'pins_week')").fetchone()[0] == 3


# -- Home Assistant -------------------------------------------------------------------------------

def test_next_and_ready_sensors(lib):
    lib.execute("UPDATE media_items SET added_at = ?, guid = 'plex://episode/1' WHERE rating_key = 101", (int(NOW) - D,))
    lib.execute("INSERT INTO media_items (rating_key, server_id, type, guid, title, grandparent_rating_key, season_index, "
                "episode_index, added_at) VALUES (102, 1, 'episode', 'plex://episode/2', 'Two', 10, 1, 2, ?)", (int(NOW) - 2 * H,))
    nxt = pins.next_up(lib, OWNER, NOW)
    assert (nxt["episode"], nxt["state"]) == (4, "airing_today")
    st = mqtt.pins_state(lib, OWNER, NOW)
    assert st["next"] == "Pinned Show S01E04" and st["ready"] == 1
    assert st["ready_attrs"]["shows"] == ["Pinned Show · next S01E01 (2 waiting)"]
    assert mqtt.pins_state(lib, 999, NOW) == {"next": "none", "next_attrs": {"air_at": None, "title": None, "state": None},
                                              "ready": 0, "ready_attrs": {"shows": []}}


def test_discovery_has_the_pin_sensors():
    cfg = {"mqtt.base_topic": "bingestats", "mqtt.discovery_prefix": "homeassistant"}
    out = mqtt.discovery(cfg, [{"id": 7, "username": "alexp", "friendly_name": "Alex"}], "Plex")
    nxt = json.loads(out["homeassistant/sensor/bingestats/user_7_next_pinned/config"])
    ready = json.loads(out["homeassistant/sensor/bingestats/user_7_pinned_ready/config"])
    assert nxt["state_topic"] == ready["state_topic"] == "bingestats/user/7/pins"
    assert nxt["name"] == "Alex next pinned" and ready["unit_of_measurement"] == "shows"
    gone = mqtt.removed_user_topics(cfg, 7)
    assert "homeassistant/sensor/bingestats/user_7_pinned_ready/config" in gone and "bingestats/user/7/pins" in gone


def test_worker_sends_arrivals_and_queued_moves(lib):
    from bingestats.worker.main import Worker
    w = Worker.__new__(Worker)
    sent = []
    w.conn, w.client, w.poller = lib, None, None

    class N:
        def handle(self, trigger, event):
            sent.append((trigger, event.get("pinner", {}).get("id")))
    w.notifier = N()
    lib.execute("INSERT INTO recently_added_queue (rating_key, queued_at) VALUES (101, ?)", (int(NOW) - 3600,))
    lib.execute("INSERT INTO pin_events (trigger, payload, created_at) VALUES ('pin_schedule_changed', ?, ?)",
                (json.dumps({"pinner": {"id": OWNER, "name": "Marc"}, "show": "X", "count": 1}), int(NOW)))
    settings.set(lib, "pins.last_week_at", 10**10)                      # not Monday's job here
    w.recently_added()
    assert ("recently_added", None) in sent
    assert ("pin_arrived", OWNER) in sent and ("pin_arrived", ALEX) in sent
    assert ("pin_schedule_changed", OWNER) in sent
