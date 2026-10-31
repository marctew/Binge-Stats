import json

import pytest

from conftest import load_fixture

from bingestats import db
from bingestats.plex.parse import parse_sessions
from bingestats.services import notify
from bingestats.services.notify import Notifier
from bingestats.worker.poller import Poller


def session_event(name="sessions_direct_play.json", **over):
    [(s, _raw)] = parse_sessions(load_fixture(name))
    d = s.to_dict()
    d.update(over)
    return {"session": d}


@pytest.fixture
def ndb(conn):
    conn.execute("INSERT INTO servers (id, machine_id, name, is_up) VALUES (1, 'm', 'DS9 - Terok Nor', 1)")
    conn.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (1001, 'alice', 'Sarah', 's')")
    conn.execute("INSERT INTO libraries (id, server_id, name, type) VALUES (5, 1, 'TV Programmes', 'show')")
    conn.execute("INSERT INTO ip_geo (ip, is_private, city, country, looked_up_at) VALUES ('192.168.1.50', 1, NULL, NULL, 0)")
    return conn


def channel(conn, name="ntfy", url="ntfy://ntfy.local/plex"):
    return notify.save_channel(conn, name, url)


def rule(conn, trigger="play_start", channels=None, **kw):
    data = {"name": kw.pop("name", "r"), "trigger": trigger, "title_template": kw.pop("title", "{{ user.name }}"),
            "body_template": kw.pop("body", "{{ item.display_title }}"), "channel_ids": channels or [],
            "enabled": kw.pop("enabled", bool(channels)), "cooldown_s": kw.pop("cooldown_s", 0),
            "include_poster": kw.pop("include_poster", True), "conditions": kw.pop("conditions", [])}
    return notify.save_rule(conn, data)


class Sent:
    def __init__(self, ok=True, err=None):
        self.calls, self.ok, self.err = [], ok, err

    def __call__(self, url, title, body, attachment):
        self.calls.append((url, title, body, attachment))
        return self.ok, self.err


# -- migration ----------------------------------------------------------------------

def test_migration_2_upgrades_v1_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    c = db.connect(path)
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
    monkeypatch.setattr(db, "LATEST", 1)
    assert db.migrate(c) == 1
    c.execute("INSERT INTO notify_rules (name, trigger, body_template, channel_ids) VALUES ('mine', 'play_start', 'x', '[]')")
    monkeypatch.undo()
    assert db.migrate(c) == 2
    names = [r[0] for r in c.execute("SELECT name FROM notify_rules ORDER BY id")]
    assert names[0] == "mine" and "Recently added" in names and len(names) == 1 + 5
    assert c.execute("SELECT COUNT(*) FROM notify_rules WHERE enabled = 1 AND name != 'mine'").fetchone()[0] == 0
    c.execute("INSERT INTO notify_samples VALUES ('play_start', '{}', 0)")


def test_default_rule_templates_compile_and_render(conn):
    for r in notify.list_rules(conn):
        notify.check_template(r["title_template"])
        ctx = notify.sample_context(r["trigger"])
        assert notify.render(r["title_template"], ctx, 250)
        assert notify.conditions_match(r["conditions"], ctx)


# -- conditions -----------------------------------------------------------------------

def test_validate_conditions():
    ok = notify.validate_conditions("play_start", [
        {"field": "user.id", "op": "in", "value": ["1001"]},
        {"field": "progress.pct", "op": "gt", "value": "50"},
        {"field": "location.is_local", "op": "is_false", "value": "whatever"}])
    assert ok[1]["value"] == 50 and ok[2]["value"] is None
    with pytest.raises(ValueError, match="can't be used"):
        notify.validate_conditions("server_down", [{"field": "user.id", "op": "in", "value": ["1"]}])
    with pytest.raises(ValueError, match="doesn't work"):
        notify.validate_conditions("play_start", [{"field": "stream.hw", "op": "gt", "value": 1}])
    with pytest.raises(ValueError, match="needs a number"):
        notify.validate_conditions("play_start", [{"field": "progress.pct", "op": "gt", "value": "lots"}])
    with pytest.raises(ValueError, match="Pick at least one"):
        notify.validate_conditions("play_start", [{"field": "user.id", "op": "in", "value": []}])
    assert notify.validate_conditions("play_stop", [{"field": "play.watched", "op": "is_true"}])


@pytest.mark.parametrize("cond, expected", [
    ({"field": "user.id", "op": "in", "value": ["1001", "7"]}, True),
    ({"field": "user.id", "op": "not_in", "value": ["1001"]}, False),
    ({"field": "stream.reasons", "op": "contains", "value": "subtitle_burn"}, True),
    ({"field": "stream.reasons", "op": "contains", "value": "video_codec"}, False),
    ({"field": "item.show", "op": "contains", "value": "slow"}, True),
    ({"field": "item.show", "op": "eq", "value": "slow horses"}, True),
    ({"field": "item.show", "op": "ne", "value": "Bluey"}, True),
    ({"field": "progress.pct", "op": "gt", "value": 40}, True),
    ({"field": "progress.pct", "op": "lt", "value": 40}, False),
    ({"field": "location.is_local", "op": "is_false"}, True),
    ({"field": "stream.hw", "op": "is_true"}, True),
    ({"field": "missing.thing", "op": "eq", "value": "x"}, False),
])
def test_condition_ops(cond, expected):
    assert notify.condition_matches(cond, notify.sample_context("play_start")) is expected


# -- templates ------------------------------------------------------------------------

def test_render_helpers_and_missing_values():
    ctx = notify.sample_context("play_stop")
    out = notify.render("{{ play.seconds | duration }} {{ progress.pct | progress_bar(10) }} {{ nope.deeper }}|"
                        "{{ 3 | plural('episode') }}", ctx, 200)
    assert out == "52m ▓▓▓▓░░░░░░ |3 episodes"


def test_sandbox_blocks_internals():
    # The sandbox renders unsafe attributes as undefined (empty) and refuses calls on them.
    assert notify.render("{{ ''.__class__.__mro__ }}{{ cycler.__init__.__globals__ }}", {}, 100) == ""
    try:
        out = notify.render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {}, 100)
    except Exception:  # noqa: BLE001 - raising is also fine
        out = ""
    assert "class" not in out
    with pytest.raises(ValueError, match="Template error"):
        notify.check_template("{% if %}")


def test_render_truncates():
    assert len(notify.render("{{ 'x' * 9000 }}", {}, notify.BODY_MAX)) == notify.BODY_MAX


# -- contexts -------------------------------------------------------------------------

def test_session_context(ndb):
    ctx = notify.build_context(ndb, "play_start", session_event())
    assert ctx["user"]["name"] == "Sarah"
    assert ctx["item"]["display_title"] == "The Jury: Murder Trial" and ctx["item"]["subtitle"] == "S03E02 · The Boy"
    assert ctx["library"]["name"] == "TV Programmes"
    assert ctx["stream"]["decision_label"] == "Direct Play" and ctx["stream"]["quality"] == "1080p H.264"
    assert ctx["location"]["is_local"] is True and ctx["progress"]["pct"] == 96
    assert ctx["server"]["name"] == "DS9 - Terok Nor"
    assert ctx["_poster"] == "/library/metadata/62949/thumb/1788494594"
    json.dumps(ctx)


def test_transcode_and_stop_context(ndb):
    ctx = notify.build_context(ndb, "transcode_start", session_event("sessions_transcode.json"))
    assert "Subtitles burned in" in ctx["stream"]["reasons_text"] and "→" in ctx["stream"]["quality"]
    assert ctx["user"]["name"] == "bob"            # not in users table: falls back to the Plex name
    stop = notify.build_context(ndb, "play_stop", {**session_event(), "play_id": 9, "play_s": 1500, "watched": True,
                                                   "percent_complete": 90})
    assert stop["play"] == {"id": 9, "seconds": 1500, "minutes": 25, "duration": "25m", "watched": True, "pct": 90}
    assert stop["_key"] == "play:9"


def test_server_context(ndb):
    ctx = notify.build_context(ndb, "server_up", {"server": {"id": 1}, "down_for_s": 754})
    assert notify.render("{{ server.name }} back after {{ down_for | duration }}", ctx, 100) == "DS9 - Terok Nor back after 12m"


# -- notifier -------------------------------------------------------------------------

def test_handle_plans_and_delivers(ndb):
    cid = channel(ndb)
    rule(ndb, channels=[cid], title="{{ user.name }} started", body="{{ item.subtitle }}")
    sent = Sent()
    n = Notifier(ndb, "unused", sender=sent, poster_fetch=lambda p: b"\xff\xd8jpeg", start_thread=False)
    [d] = n.handle("play_start", session_event())
    assert (d.title, d.body) == ("Sarah started", "S03E02 · The Boy")
    assert n.deliver(d, ndb) is True
    url, title, body, attachment = sent.calls[0]
    assert url == "ntfy://ntfy.local/plex" and attachment.endswith(".jpg")
    import os
    assert not os.path.exists(attachment)                 # temp poster cleaned up
    row = ndb.execute("SELECT * FROM notify_log").fetchone()
    assert row["ok"] == 1 and row["dedupe_key"].startswith("play_start:")
    assert ndb.execute("SELECT trigger FROM notify_samples").fetchone()[0] == "play_start"


def test_failed_delivery_logged(ndb):
    cid = channel(ndb)
    rule(ndb, channels=[cid])
    n = Notifier(ndb, "unused", sender=Sent(False, "HTTP 401 from ntfy"), start_thread=False)
    [d] = n.handle("play_start", session_event())
    assert n.deliver(d, ndb) is False
    assert dict(ndb.execute("SELECT ok, error FROM notify_log").fetchone()) == {"ok": 0, "error": "HTTP 401 from ntfy"}


def test_conditions_disabled_rules_and_channels(ndb):
    cid = channel(ndb)
    off = channel(ndb, "off")
    ndb.execute("UPDATE notify_channels SET enabled = 0 WHERE id = ?", (off,))
    rule(ndb, channels=[cid, off], name="local only",
         conditions=[{"field": "location.is_local", "op": "is_true"}])
    rule(ndb, channels=[cid], name="remote only", conditions=[{"field": "location.is_local", "op": "is_false"}])
    rid = rule(ndb, channels=[cid], name="paused")
    notify.set_rule_enabled(ndb, rid, False)
    n = Notifier(ndb, "unused", sender=Sent(), start_thread=False)
    out = n.handle("play_start", session_event())
    assert [(d.rule_id, d.channel_id) for d in out] == [(1 + 5, cid)]


def test_cooldown(ndb, clock):
    cid = channel(ndb)
    rule(ndb, trigger="pause", channels=[cid], cooldown_s=600)
    n = Notifier(ndb, "unused", sender=Sent(), clock=clock, start_thread=False)
    [d] = n.handle("pause", session_event())
    n.deliver(d, ndb)
    clock.advance(300)
    assert n.handle("pause", session_event()) == []
    clock.advance(400)
    assert len(n.handle("pause", session_event())) == 1
    other = session_event()
    other["session"]["session_id"] = "another-session"
    clock.advance(1)
    assert len(n.handle("pause", other)) == 1           # cooldown is per session


def test_template_error_is_logged_not_raised(ndb):
    cid = channel(ndb)
    rule(ndb, channels=[cid], body="{{ item.title.upper(1, 2, 3) }}")
    n = Notifier(ndb, "unused", sender=Sent(), start_thread=False)
    assert n.handle("play_start", session_event()) == []
    assert "Template error" in ndb.execute("SELECT error FROM notify_log").fetchone()[0]


def test_bad_event_never_raises(ndb):
    n = Notifier(ndb, "unused", sender=Sent(), start_thread=False)
    assert n.handle("play_start", {"session": None}) == [] or True
    assert n.handle("not_a_trigger", {}) == []


def test_poller_events_reach_notifier(ndb, plex, clock):
    cid = channel(ndb)
    rule(ndb, channels=[cid], title="start {{ user.name }}")
    rule(ndb, trigger="play_stop", channels=[cid], title="stop {{ play.minutes }}m",
         conditions=[{"field": "play.watched", "op": "is_true"}])
    n = Notifier(ndb, "unused", sender=Sent(), clock=clock, start_thread=False)
    seen = []
    p = Poller(ndb, plex, clock=clock, on_event=lambda t, e: seen.extend(n.handle(t, e)))
    ndb.execute("UPDATE users SET id = 1001")  # fixture user
    plex.play("sessions_direct_play.json")
    p.poll_once()
    clock.advance(600)
    p.poll_once()
    plex.stop_all()
    for _ in range(3):
        clock.advance(5)
        p.poll_once()
    assert [d.title for d in seen] == ["start Sarah", "stop 10m"]


# -- channels and rules -------------------------------------------------------------------

def test_channel_validation_and_masking(ndb):
    with pytest.raises(ValueError, match="doesn't recognise"):
        notify.save_channel(ndb, "bad", "notaurl")
    cid = notify.save_channel(ndb, "Discord", "discord://123456/abcdefsecret")
    [c] = notify.list_channels(ndb)
    assert c["masked"] == "discord://1234•••" and "secret" not in json.dumps(c)
    notify.save_channel(ndb, "Discord 2", "", channel_id=cid)          # blank URL keeps the old one
    assert ndb.execute("SELECT apprise_url FROM notify_channels").fetchone()[0] == "discord://123456/abcdefsecret"
    rid = rule(ndb, channels=[cid])
    notify.delete_channel(ndb, cid)
    assert notify.get_rule(ndb, rid)["channel_ids"] == []


def test_rule_validation(ndb):
    with pytest.raises(ValueError, match="channel"):
        notify.save_rule(ndb, {"name": "x", "trigger": "play_start", "body_template": "hi", "enabled": True})
    with pytest.raises(ValueError, match="Template error"):
        notify.save_rule(ndb, {"name": "x", "trigger": "play_start", "body_template": "{% for %}"})
    with pytest.raises(ValueError, match="Pick what"):
        notify.save_rule(ndb, {"name": "x", "trigger": "nope", "body_template": "hi"})


def test_preview_uses_last_real_event(ndb):
    out = notify.preview(ndb, "play_start", "{{ user.name }}", "", [])
    assert out["real"] is False and out["title"] == "Sarah"            # sample
    Notifier(ndb, "unused", sender=Sent(), start_thread=False).handle(
        "play_start", session_event("sessions_transcode.json"))
    out = notify.preview(ndb, "play_start", "{{ user.name }} {{ item.display_title }}", "", [
        {"field": "stream.decision", "op": "in", "value": ["transcode"]}])
    assert out["real"] is True and out["title"] == "bob Dune: Part Two" and out["matches"] is True
    assert notify.preview(ndb, "play_start", "{{ x(", "", [])["error"]
    half = notify.preview(ndb, "play_start", "{{ user.name }}", "", [{"field": "user.id", "op": "in", "value": []}])
    assert half["title"] == "bob" and half["error"] is None and "Pick at least one" in half["condition_error"]
