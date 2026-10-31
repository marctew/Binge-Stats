import json

from bingestats.services import notify


def test_index_renders_with_defaults(logged_in):
    body = logged_in.get("/notifications").data.decode()
    assert "Someone started watching" in body and "Recently added" in body
    assert "Add a channel first" in body


def test_channel_lifecycle(logged_in, app_conn, monkeypatch):
    logged_in.get("/notifications")
    r = logged_in.post("/notifications/channels", {"name": "Phone", "url": "ntfys://marc:s3cretpass@ntfy.example/plex"})
    assert r.status_code == 302
    body = logged_in.get("/notifications").data.decode()
    assert "Phone" in body and "s3cretpass" not in body and "ntfys://marc•••" in body
    cid = app_conn.execute("SELECT id FROM notify_channels").fetchone()[0]
    sent = []
    monkeypatch.setattr(notify, "send_one", lambda url, t, b, a=None: (sent.append(url) or True, None))
    r = logged_in.post(f"/notifications/channels/{cid}/test", {}, headers={"HX-Request": "true"})
    assert b"Test sent to Phone" in r.data and sent == ["ntfys://marc:s3cretpass@ntfy.example/plex"]
    monkeypatch.setattr(notify, "send_one", lambda url, t, b, a=None: (False, "connection refused"))
    r = logged_in.post(f"/notifications/channels/{cid}/test", {}, headers={"HX-Request": "true"})
    assert b"connection refused" in r.data
    logged_in.post("/notifications/channels", {"name": "Bad", "url": "nonsense"})
    assert app_conn.execute("SELECT COUNT(*) FROM notify_channels").fetchone()[0] == 1


def test_rule_editor_save_toggle_delete(logged_in, app_conn):
    cid = notify.save_channel(app_conn, "Phone", "ntfy://192.168.4.142/plex")
    page = logged_in.get("/notifications/rules/new?trigger=play_stop").data.decode()
    assert 'id="rule-data"' in page and "play.watched" in page and "{{ user.name }}" not in page.split("rule-data")[0]
    conds = json.dumps([{"field": "play.watched", "op": "is_true", "value": None}])
    form = {"name": "Finished", "trigger": "play_stop", "conditions": conds, "title_template": "{{ user.name }} finished",
            "body_template": "{{ item.display_title }}", "channel_ids": str(cid), "cooldown_s": "0", "enabled": "on"}
    r = logged_in.post("/notifications/rules/new", form)
    assert r.status_code == 302
    rid = app_conn.execute("SELECT id FROM notify_rules WHERE name = 'Finished'").fetchone()[0]
    rule = notify.get_rule(app_conn, rid)
    assert rule["enabled"] == 1 and rule["channel_ids"] == [cid] and rule["conditions"][0]["field"] == "play.watched"
    assert logged_in.get(f"/notifications/rules/{rid}").status_code == 200
    logged_in.post(f"/notifications/rules/{rid}/toggle", {"enabled": "0"})
    assert notify.get_rule(app_conn, rid)["enabled"] == 0
    logged_in.post(f"/notifications/rules/{rid}/delete", {})
    assert notify.get_rule(app_conn, rid) is None


def test_rule_editor_shows_errors(logged_in, app_conn):
    logged_in.get("/notifications/rules/new")
    r = logged_in.post("/notifications/rules/new", {"name": "x", "trigger": "play_start", "conditions": "[]",
                                                    "title_template": "{% if %}", "body_template": "", "enabled": "on"})
    assert r.status_code == 400 and b"Template error" in r.data


def test_cannot_enable_default_rule_without_channel(logged_in, app_conn):
    rid = app_conn.execute("SELECT id FROM notify_rules WHERE trigger = 'play_start'").fetchone()[0]
    logged_in.get("/notifications")
    logged_in.post(f"/notifications/rules/{rid}/toggle", {"enabled": "1"})
    assert notify.get_rule(app_conn, rid)["enabled"] == 0


def test_preview_endpoint(logged_in):
    logged_in.get("/notifications/rules/new")
    r = logged_in.post("/notifications/preview", {"trigger": "play_start", "title_template": "{{ user.name }} on {{ player.product }}",
                                                  "body_template": "", "conditions": "[]"})
    assert b"Sarah on Plex for LG" in r.data and b"an example" in r.data
    r = logged_in.post("/notifications/preview", {"trigger": "play_start", "title_template": "{{ broken(",
                                                  "body_template": "", "conditions": "[]"})
    assert b"Error" in r.data
