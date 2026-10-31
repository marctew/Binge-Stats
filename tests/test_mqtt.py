"""Home Assistant over MQTT (SPEC.md section 14), with a fake paho client."""
import json
from types import SimpleNamespace

import pytest

from bingestats.services import homeassistant, settings
from bingestats.worker.mqtt import REPUBLISH_S, MqttBridge
from bingestats.worker.poller import Poller


class FakeMqtt:
    instances: list = []

    def __init__(self, client_id):
        self.client_id = client_id
        self.published: list[tuple[str, str, bool]] = []
        self.subscribed: list[str] = []
        self.will = None
        self.auth = None
        self.tls = False
        self.target = None
        self.running = False
        FakeMqtt.instances.append(self)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def username_pw_set(self, u, p):
        self.auth = (u, p)

    def tls_set(self):
        self.tls = True

    def reconnect_delay_set(self, lo, hi):
        pass

    def connect_async(self, host, port, keepalive=60):
        self.target = (host, port)

    def loop_start(self):
        self.running = True

    def loop_stop(self):
        self.running = False

    def disconnect(self):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    # test helpers
    def connect_ok(self):
        self.on_connect(self, None, {}, SimpleNamespace(is_failure=False), None)

    def deliver(self, topic, payload=b"PRESS"):
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload))

    def last(self, topic):
        found = [p for t, p, _r in self.published if t == topic]
        return found[-1] if found else None

    def topics(self):
        return [t for t, _p, _r in self.published]


@pytest.fixture
def stopped():
    return []


@pytest.fixture
def bridge(conn, stopped):
    FakeMqtt.instances = []
    woke = []
    b = MqttBridge(conn, stop_user=lambda uid: stopped.append(uid) or 2, client_factory=FakeMqtt,
                   wake=lambda: woke.append(1))
    b.woke = woke
    return b


def enable(conn, **extra):
    settings.set(conn, "mqtt.enabled", True)
    settings.set(conn, "mqtt.host", "192.168.4.121")
    for k, v in extra.items():
        settings.set(conn, f"mqtt.{k}", v)


@pytest.fixture
def live(conn, plex, clock, tmp_path):
    """Two sessions playing (alice direct play on LAN, bob transcoding on WAN)."""
    plex.play("sessions_direct_play.json", "sessions_transcode.json")
    Poller(conn, plex, clock=clock, data_dir=tmp_path).poll_once()
    return conn


def test_off_by_default_and_no_host(conn, bridge):
    bridge.tick(1000)
    assert FakeMqtt.instances == [] and settings.get(conn, "mqtt.status") == "off"
    settings.set(conn, "mqtt.enabled", True)
    bridge.tick(1001)
    assert FakeMqtt.instances == [] and settings.get(conn, "mqtt.status") == "No broker host set"


def test_connects_with_lwt_auth_and_tls(conn, bridge):
    enable(conn, username="bingestats", password="pw", tls=True, port=8883)
    bridge.tick(1000)
    [c] = FakeMqtt.instances
    assert c.target == ("192.168.4.121", 8883) and c.auth == ("bingestats", "pw") and c.tls and c.running
    assert c.will == ("bingestats/status", "offline", True)
    assert settings.get(conn, "mqtt.status").startswith("Connecting")
    c.connect_ok()
    assert c.subscribed == ["bingestats/cmd/#"] and c.last("bingestats/status") == "online"
    bridge.tick(1001)
    assert settings.get(conn, "mqtt.status") == "connected"


def test_discovery_and_state(live, bridge):
    conn = live
    enable(conn)
    bridge.tick(1000)
    c = FakeMqtt.instances[0]
    c.connect_ok()
    bridge.tick(1001)
    configs = {t: json.loads(p) for t, p, r in c.published if t.endswith("/config")}
    assert all(r for t, _p, r in c.published)                       # everything retained
    streams = configs["homeassistant/sensor/bingestats/active_streams/config"]
    assert streams["state_topic"] == "bingestats/state" and streams["json_attributes_topic"] == "bingestats/sessions"
    assert streams["availability_topic"] == "bingestats/status"
    assert streams["device"]["name"] == "Binge Stats" and streams["device"]["model"] == "Test Plex"
    assert configs["homeassistant/binary_sensor/bingestats/plex/config"]["device_class"] == "connectivity"
    assert configs["homeassistant/sensor/bingestats/bandwidth_wan/config"]["unit_of_measurement"] == "Mbit/s"
    assert len({c["unique_id"] for c in configs.values()}) == len(configs) == 11   # no users tracked yet

    state = json.loads(c.last("bingestats/state"))
    assert state["streams"] == 2 and state["transcode"] == 1 and state["direct_play"] == 1
    assert state["bandwidth_mbps"] == pytest.approx(14.7) and state["wan_mbps"] == pytest.approx(9.2)
    assert state["plex_up"] is True and state["alerts"] == 0 and state["plays_today"] == 0
    sessions = json.loads(c.last("bingestats/sessions"))["sessions"]
    assert {s["user"] for s in sessions} == {"alice", "bob"}


def test_publishes_on_change_and_every_minute(live, bridge, plex, clock):
    conn = live
    enable(conn)
    bridge.tick(1000)
    c = FakeMqtt.instances[0]
    c.connect_ok()
    bridge.tick(1001)
    n = len(c.published)
    bridge.tick(1005)
    assert len(c.published) == n                                     # nothing changed
    conn.execute("INSERT INTO alerts (created_at, kind, detail) VALUES (1, 'server_down', '{}')")
    bridge.tick(1010)
    assert c.topics()[n:] == ["bingestats/state"] and json.loads(c.last("bingestats/state"))["alerts"] == 1
    bridge.tick(1001 + REPUBLISH_S)
    again = c.topics()[n + 1:]
    assert "bingestats/state" in again and "bingestats/sessions" in again
    assert not any(t.endswith("/config") for t in again)             # discovery only when it changes


def test_tracked_users_get_sensor_and_button_and_are_removed(live, bridge):
    conn = live
    enable(conn, tracked_users=[1001])
    conn.execute("UPDATE users SET friendly_name = 'Sarah' WHERE id = 1001")
    bridge.tick(1000)
    c = FakeMqtt.instances[0]
    c.connect_ok()
    bridge.tick(1001)
    sensor = json.loads(c.last("homeassistant/sensor/bingestats/user_1001_watching/config"))
    button = json.loads(c.last("homeassistant/button/bingestats/user_1001_stop/config"))
    assert sensor["name"] == "Sarah watching" and sensor["state_topic"] == "bingestats/user/1001"
    assert button["name"] == "Stop Sarah's streams" and button["command_topic"] == "bingestats/cmd/stop_user/1001"
    watching = json.loads(c.last("bingestats/user/1001"))
    assert watching["state"] == "The Jury: Murder Trial S03E02" and watching["decision"] == "direct_play"
    assert watching["streams"] == 1 and 0 <= watching["progress"] <= 100
    assert settings.get(conn, "mqtt.published_users") == [1001]

    settings.set(conn, "mqtt.tracked_users", [])
    bridge.tick(1002)
    assert c.last("homeassistant/sensor/bingestats/user_1001_watching/config") == ""
    assert c.last("homeassistant/button/bingestats/user_1001_stop/config") == ""
    assert c.last("bingestats/user/1001") == ""
    assert settings.get(conn, "mqtt.published_users") == []


def test_idle_user(conn, bridge):
    conn.execute("INSERT INTO users (id, username, share_salt) VALUES (7, 'kid', 's')")
    enable(conn, tracked_users=[7])
    bridge.tick(1000)
    FakeMqtt.instances[0].connect_ok()
    bridge.tick(1001)
    assert json.loads(FakeMqtt.instances[0].last("bingestats/user/7")) == {"state": "idle", "streams": 0, "sessions": []}


def test_stop_button_only_for_tracked_users(conn, bridge, stopped):
    conn.execute("INSERT INTO users (id, username, share_salt) VALUES (7, 'kid', 's')")
    enable(conn, tracked_users=[7])
    bridge.tick(1000)
    c = FakeMqtt.instances[0]
    c.connect_ok()
    c.deliver("bingestats/cmd/stop_user/7")
    c.deliver("bingestats/cmd/stop_user/8")
    c.deliver("bingestats/cmd/something_else")
    assert bridge.woke == [1, 1]
    bridge.tick(1001)
    assert stopped == [7]


def test_settings_change_reconnects_and_disable_goes_offline(conn, bridge):
    enable(conn)
    bridge.tick(1000)
    first = FakeMqtt.instances[0]
    first.connect_ok()
    bridge.tick(1001)
    settings.set(conn, "mqtt.port", 1884)
    bridge.tick(1002)
    assert len(FakeMqtt.instances) == 2 and first.last("bingestats/status") == "offline" and not first.running
    FakeMqtt.instances[1].connect_ok()
    settings.set(conn, "mqtt.enabled", False)
    bridge.tick(1003)
    assert FakeMqtt.instances[1].last("bingestats/status") == "offline"
    assert settings.get(conn, "mqtt.status") == "off"


def test_reconnect_republishes_everything(conn, bridge):
    enable(conn)
    bridge.tick(1000)
    c = FakeMqtt.instances[0]
    c.connect_ok()
    bridge.tick(1001)
    n = len(c.published)
    c.on_disconnect(c, None, {}, "network", None)
    assert settings.get(conn, "mqtt.status") == "connected"          # written on the next tick
    bridge.tick(1002)
    assert settings.get(conn, "mqtt.status").startswith("Disconnected")
    c.connect_ok()
    bridge.tick(1003)
    assert sum(1 for t in c.topics()[n:] if t.endswith("/config")) == 11


def test_refused_connection_shows_reason(conn, bridge):
    enable(conn)
    bridge.tick(1000)
    c = FakeMqtt.instances[0]
    c.on_connect(c, None, {}, SimpleNamespace(is_failure=True, __str__=lambda s: "Not authorized"), None)
    bridge.tick(1001)
    assert settings.get(conn, "mqtt.status").startswith("Broker refused")
    assert not any(t.endswith("/config") for t in c.topics())


# -- settings ------------------------------------------------------------------------------

class Form(dict):
    def getlist(self, k):
        v = self.get(k, [])
        return v if isinstance(v, list) else [v]


def test_list_settings_round_trip(conn):
    assert settings.get(conn, "mqtt.tracked_users") == []
    settings.get(conn, "mqtt.tracked_users").append(5)                # a caller mutating the default
    assert settings.get(conn, "mqtt.tracked_users") == []
    settings.set(conn, "mqtt.tracked_users", ["3", 1, 3])
    assert settings.get(conn, "mqtt.tracked_users") == [1, 3]


def test_homeassistant_save_validates_and_keeps_password(conn):
    conn.execute("INSERT INTO users (id, username, share_salt) VALUES (7, 'kid', 's')")
    form = Form({"mqtt.enabled": "on", "mqtt.host": " 192.168.4.121 ", "mqtt.port": "1883", "mqtt.username": "bs",
                 "mqtt.password": "secret", "mqtt.base_topic": "bingestats", "mqtt.discovery_prefix": "homeassistant",
                 "tracked_users": ["7"]})
    homeassistant.save(conn, form)
    assert settings.get(conn, "mqtt.host") == "192.168.4.121" and settings.get(conn, "mqtt.password") == "secret"
    assert settings.get(conn, "mqtt.tracked_users") == [7]
    homeassistant.save(conn, Form({**form, "mqtt.password": ""}))
    assert settings.get(conn, "mqtt.password") == "secret"
    v = homeassistant.view(conn)
    assert v["has_password"] and "mqtt.password" not in v and v["users"][0]["tracked"]
    homeassistant.save(conn, Form({**form, "mqtt.password": "", "clear_password": "on"}))
    assert settings.get(conn, "mqtt.password") == ""
    for bad, msg in [({"mqtt.host": ""}, "host"), ({"mqtt.port": "99999"}, "Port"),
                     ({"mqtt.base_topic": "bingestats/#"}, "topic"), ({"mqtt.discovery_prefix": "/ha"}, "topic"),
                     ({"tracked_users": ["42"]}, "Unknown user")]:
        with pytest.raises(ValueError, match=msg):
            homeassistant.save(conn, Form({**form, **bad}))
    assert settings.get(conn, "mqtt.base_topic") == "bingestats"


def test_settings_page_section(logged_in, app_conn):
    app_conn.execute("INSERT INTO users (id, username, friendly_name, share_salt) VALUES (7, 'kid', 'Ollie', 's')")
    body = logged_in.get("/settings").data.decode()
    assert "Home Assistant" in body and "Ollie" in body
    r = logged_in.post("/settings/homeassistant", {"mqtt.enabled": "on", "mqtt.host": "mqtt.local", "mqtt.port": "1883",
                                                   "mqtt.password": "pw", "mqtt.base_topic": "bingestats",
                                                   "mqtt.discovery_prefix": "homeassistant", "tracked_users": "7"})
    assert r.status_code == 302 and r.headers["Location"].endswith("#homeassistant")
    assert settings.get(app_conn, "mqtt.tracked_users") == [7]
    body = logged_in.get("/settings").data.decode()
    assert "pw" not in body.split('id="mqtt_pass"')[1][:300] and "leave blank to keep" in body
