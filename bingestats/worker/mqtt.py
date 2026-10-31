"""Home Assistant over MQTT (SPEC.md section 14).

The worker owns this. Each pass of its loop calls `MqttBridge.tick()`, which:

- (re)connects when the mqtt.* settings change, or disconnects when turned off;
- publishes HA discovery configs (and empties them for users no longer tracked);
- publishes state when it changes, and everything again every 60 s;
- runs "stop streams" button presses that arrived since the last pass.

paho runs its network loop on its own thread. Its callbacks never touch the
database: they set flags and queue commands, and `tick()` acts on them on the
worker thread. A command also calls `wake()` so the worker doesn't sit out
the rest of its poll interval.

Topics, with base = mqtt.base_topic:
  {base}/status           online / offline (retained, LWT)
  {base}/state            server-wide counts as JSON
  {base}/sessions         {"sessions": [...]} attributes for Active streams
  {base}/user/{id}        one tracked user's now-watching JSON
  {base}/cmd/stop_user/{id}   button press -> stop that user's streams
"""
from __future__ import annotations

import json
import logging
import queue
import re
import sqlite3
import time
from typing import Any, Callable

from .. import __version__
from ..services import activity, alerts, settings, stats

log = logging.getLogger("bingestats.mqtt")

REPUBLISH_S = 60
PAYLOAD_PRESS = "PRESS"

# key, name, component, extra discovery fields
SERVER_ENTITIES: list[tuple[str, str, str, dict]] = [
    ("active_streams", "Active streams", "sensor",
     {"value_template": "{{ value_json.streams }}", "unit_of_measurement": "streams", "state_class": "measurement",
      "icon": "mdi:play-network", "json_attributes_topic": "{base}/sessions"}),
    ("transcodes", "Transcodes", "sensor",
     {"value_template": "{{ value_json.transcode }}", "unit_of_measurement": "streams", "state_class": "measurement",
      "icon": "mdi:cog-transfer"}),
    ("direct_plays", "Direct plays", "sensor",
     {"value_template": "{{ value_json.direct_play }}", "unit_of_measurement": "streams",
      "state_class": "measurement", "icon": "mdi:play-circle"}),
    ("direct_streams", "Direct streams", "sensor",
     {"value_template": "{{ value_json.direct_stream }}", "unit_of_measurement": "streams",
      "state_class": "measurement", "icon": "mdi:play-circle-outline"}),
    ("bandwidth", "Bandwidth", "sensor",
     {"value_template": "{{ value_json.bandwidth_mbps }}", "unit_of_measurement": "Mbit/s",
      "device_class": "data_rate", "state_class": "measurement", "suggested_display_precision": 1}),
    ("bandwidth_lan", "Bandwidth LAN", "sensor",
     {"value_template": "{{ value_json.lan_mbps }}", "unit_of_measurement": "Mbit/s",
      "device_class": "data_rate", "state_class": "measurement", "suggested_display_precision": 1}),
    ("bandwidth_wan", "Bandwidth WAN", "sensor",
     {"value_template": "{{ value_json.wan_mbps }}", "unit_of_measurement": "Mbit/s",
      "device_class": "data_rate", "state_class": "measurement", "suggested_display_precision": 1}),
    ("plex", "Plex server", "binary_sensor",
     {"value_template": "{{ 'ON' if value_json.plex_up else 'OFF' }}", "device_class": "connectivity"}),
    ("plays_today", "Plays today", "sensor",
     {"value_template": "{{ value_json.plays_today }}", "unit_of_measurement": "plays",
      "state_class": "total_increasing", "icon": "mdi:counter"}),
    ("watch_time_today", "Watch time today", "sensor",
     {"value_template": "{{ value_json.watch_hours_today }}", "unit_of_measurement": "h",
      "device_class": "duration", "state_class": "total_increasing", "suggested_display_precision": 1}),
    ("alerts", "Unacknowledged alerts", "sensor",
     {"value_template": "{{ value_json.alerts }}", "unit_of_measurement": "alerts", "state_class": "measurement",
      "icon": "mdi:alert-circle-outline"}),
]

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "bingestats"


def _mbps(kbps: int) -> float:
    return round((kbps or 0) / 1000, 1)


def _display_title(s) -> str:
    if s.media_type == "episode" and s.grandparent_title:
        code = ""
        if s.season_index is not None and s.episode_index is not None:
            code = f" S{s.season_index:02d}E{s.episode_index:02d}"
        return f"{s.grandparent_title}{code}"
    if s.media_type == "track" and s.grandparent_title:
        return f"{s.grandparent_title} – {s.title}"
    return f"{s.title} ({s.year})" if s.title and s.year else (s.title or "Unknown")


def user_name(row) -> str:
    return row["friendly_name"] or row["username"] or f"User {row['id']}"


# -- state ----------------------------------------------------------------------------

def server_state(conn: sqlite3.Connection, active: list, now: float) -> dict:
    t = activity.totals(active)
    srv = conn.execute("SELECT is_up FROM servers ORDER BY id LIMIT 1").fetchone()
    today = stats.summary(conn, stats.make_range(conn, preset="today", now=now))
    return {
        "streams": t["streams"], "transcode": t["transcode"], "direct_play": t["direct_play"],
        "direct_stream": t["direct_stream"], "bandwidth_mbps": _mbps(t["bandwidth_kbps"]),
        "lan_mbps": _mbps(t["lan_kbps"]), "wan_mbps": _mbps(t["wan_kbps"]),
        "plex_up": bool(srv and srv["is_up"]),
        "plays_today": today["plays"], "watch_hours_today": round(today["watch_time_s"] / 3600, 2),
        "alerts": alerts.unacked_count(conn),
        **_downloads(conn),
    }


def _downloads(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT info FROM arr_status WHERE app = 'sabnzbd'").fetchone()
    q = (json.loads(row["info"]) if row else {}).get("queue") or {}
    return {"download_items": q.get("items", 0), "download_mbps": round((q.get("kbps") or 0) * 8 / 1000, 1),
            "download_status": q.get("status") or "Idle"}


# Shown only when SABnzbd is connected (key, name, extra discovery fields).
SAB_ENTITIES = [
    ("download_queue", "Download queue", {"value_template": "{{ value_json.download_items }}",
                                          "unit_of_measurement": "items", "state_class": "measurement",
                                          "icon": "mdi:download"}),
    ("download_speed", "Download speed", {"value_template": "{{ value_json.download_mbps }}",
                                          "unit_of_measurement": "Mbit/s", "device_class": "data_rate",
                                          "state_class": "measurement", "suggested_display_precision": 1}),
]


def _session_attrs(a) -> dict:
    s = a.session
    return {"user": a.user_name, "title": _display_title(s), "state": a.state, "decision": s.stream.decision,
            "player": s.player_title or s.product, "progress": round(a.progress_pct)}


def user_state(user_id: int, active: list) -> dict:
    mine = [a for a in active if a.user_id == user_id]
    if not mine:
        return {"state": "idle", "streams": 0, "sessions": []}
    # the one they're actually watching: playing beats paused, then the newest
    lead = sorted(mine, key=lambda a: (a.state == "playing", a.started_at))[-1]
    return {"state": _display_title(lead.session)[:250], "streams": len(mine),
            "progress": round(lead.progress_pct), "player": lead.session.player_title or lead.session.product,
            "decision": lead.session.stream.decision, "playback": lead.state,
            "sessions": [_session_attrs(a) for a in mine]}


# -- discovery ------------------------------------------------------------------------

def sab_topics(cfg: dict) -> list[str]:
    node = _slug(cfg["mqtt.base_topic"])
    return [f"{cfg['mqtt.discovery_prefix']}/sensor/{node}/{key}/config" for key, _n, _e in SAB_ENTITIES]


def discovery(cfg: dict, users: list, server_name: str | None, sab: bool = False) -> dict[str, str]:
    """Every discovery topic -> config payload for the current settings."""
    base, prefix = cfg["mqtt.base_topic"], cfg["mqtt.discovery_prefix"]
    node = _slug(base)
    device = {"identifiers": [node], "name": "Binge Stats", "manufacturer": "Binge Stats",
              "model": server_name or "Plex", "sw_version": __version__}
    common = {"availability_topic": f"{base}/status", "device": device}
    out: dict[str, str] = {}
    for key, name, comp, extra in SERVER_ENTITIES:
        conf = {"name": name, "unique_id": f"{node}_{key}", "object_id": f"binge_stats_{key}",
                "state_topic": f"{base}/state", **common}
        conf.update({k: (v.format(base=base) if isinstance(v, str) and "{base}" in v else v) for k, v in extra.items()})
        out[f"{prefix}/{comp}/{node}/{key}/config"] = json.dumps(conf, sort_keys=True)
    if sab:
        for (key, name, extra), topic in zip(SAB_ENTITIES, sab_topics(cfg)):
            out[topic] = json.dumps({"name": name, "unique_id": f"{node}_{key}", "object_id": f"binge_stats_{key}",
                                     "state_topic": f"{base}/state", **common, **extra}, sort_keys=True)
    for u in users:
        uid, name = u["id"], user_name(u)
        out[f"{prefix}/sensor/{node}/user_{uid}_watching/config"] = json.dumps({
            "name": f"{name} watching", "unique_id": f"{node}_user_{uid}_watching",
            "object_id": f"binge_stats_{_slug(name)}_watching", "icon": "mdi:television-play",
            "state_topic": f"{base}/user/{uid}", "value_template": "{{ value_json.state }}",
            "json_attributes_topic": f"{base}/user/{uid}", **common}, sort_keys=True)
        out[f"{prefix}/button/{node}/user_{uid}_stop/config"] = json.dumps({
            "name": f"Stop {name}'s streams", "unique_id": f"{node}_user_{uid}_stop",
            "object_id": f"binge_stats_stop_{_slug(name)}", "icon": "mdi:stop-circle-outline",
            "command_topic": f"{base}/cmd/stop_user/{uid}", "payload_press": PAYLOAD_PRESS, **common},
            sort_keys=True)
    return out


def removed_user_topics(cfg: dict, user_id: int) -> list[str]:
    base, prefix, node = cfg["mqtt.base_topic"], cfg["mqtt.discovery_prefix"], _slug(cfg["mqtt.base_topic"])
    return [f"{prefix}/sensor/{node}/user_{user_id}_watching/config",
            f"{prefix}/button/{node}/user_{user_id}_stop/config", f"{base}/user/{user_id}"]


# -- the bridge -----------------------------------------------------------------------

def paho_client(client_id: str):
    import paho.mqtt.client as mqtt
    return mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)


CONN_KEYS = ("mqtt.enabled", "mqtt.host", "mqtt.port", "mqtt.username", "mqtt.password", "mqtt.tls",
             "mqtt.base_topic", "mqtt.discovery_prefix")


class MqttBridge:
    def __init__(self, conn: sqlite3.Connection, stop_user: Callable[[int], int],
                 client_factory: Callable[[str], Any] = paho_client, wake: Callable[[], None] = lambda: None):
        self.conn = conn
        self.stop_user = stop_user
        self.client_factory = client_factory
        self.wake = wake
        self.client = None
        self._key: tuple | None = None
        self._cfg: dict = {}
        self.connected = False
        self._status = "off"
        self._just_connected = False
        self._sent: dict[str, str] = {}
        self._last_full = 0.0
        self.commands: queue.Queue[int] = queue.Queue()

    # paho thread ----------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            self.connected = False
            self._status = f"Broker refused the connection: {reason_code}"
            return
        base = self._cfg["mqtt.base_topic"]
        client.subscribe(f"{base}/cmd/#", qos=1)
        client.publish(f"{base}/status", "online", qos=1, retain=True)
        self.connected = True
        self._just_connected = True
        self._status = "connected"

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        if self._status == "connected":
            self._status = f"Disconnected ({reason_code}), retrying"

    def _on_message(self, client, userdata, msg):
        m = re.fullmatch(re.escape(self._cfg["mqtt.base_topic"]) + r"/cmd/stop_user/(\d+)", msg.topic)
        if m:
            self.commands.put(int(m.group(1)))
            self.wake()

    # worker thread ----------------------------------------------------------------------
    def _set_status(self, status: str) -> None:
        if settings.get(self.conn, "mqtt.status") != status:
            settings.set(self.conn, "mqtt.status", status)

    def _connect(self, cfg: dict) -> None:
        self._cfg = cfg
        self._sent = {}
        base = cfg["mqtt.base_topic"]
        c = self.client_factory(f"bingestats-{_slug(base)}")
        c.on_connect, c.on_disconnect, c.on_message = self._on_connect, self._on_disconnect, self._on_message
        c.will_set(f"{base}/status", "offline", qos=1, retain=True)
        if cfg["mqtt.username"]:
            c.username_pw_set(cfg["mqtt.username"], cfg["mqtt.password"] or None)
        if cfg["mqtt.tls"]:
            c.tls_set()
        c.reconnect_delay_set(1, 60)
        self._status = f"Connecting to {cfg['mqtt.host']}:{cfg['mqtt.port']}"
        try:
            c.connect_async(cfg["mqtt.host"], int(cfg["mqtt.port"]), keepalive=60)
            c.loop_start()
        except Exception as e:  # noqa: BLE001 - bad host etc; shown in Settings
            self._status = f"Can't connect: {e}"
        self.client = c
        log.info("MQTT connecting to %s:%s", cfg["mqtt.host"], cfg["mqtt.port"])

    def close(self) -> None:
        if self.client is None:
            return
        try:
            if self.connected:
                self.client.publish(f"{self._cfg['mqtt.base_topic']}/status", "offline", qos=1, retain=True)
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:  # noqa: BLE001
            log.exception("MQTT disconnect failed")
        self.client = None
        self.connected = False

    def _publish(self, topic: str, payload: str, force: bool = False) -> None:
        if not force and self._sent.get(topic) == payload:
            return
        self.client.publish(topic, payload, qos=1, retain=True)
        self._sent[topic] = payload

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        cfg = settings.get_many(self.conn, "mqtt.")
        key = tuple(cfg[k] for k in CONN_KEYS)
        if key != self._key:
            self.close()
            self._key = key
            if not cfg["mqtt.enabled"]:
                self._status = "off"
            elif not cfg["mqtt.host"]:
                self._status = "No broker host set"
            else:
                self._connect(cfg)
        self._cfg = cfg   # topics are part of the key, so they only change through _connect
        self._set_status(self._status)
        if self.client is None or not self.connected:
            self._drain_commands(None)
            return

        tracked = set(cfg["mqtt.tracked_users"])
        self._drain_commands(tracked)
        if self._just_connected:
            self._just_connected = False
            self._sent = {}
            self._last_full = 0
        force = now - self._last_full >= REPUBLISH_S
        if force:
            self._last_full = now
        users = self.conn.execute(
            "SELECT id, username, friendly_name FROM users WHERE id IN (%s) ORDER BY id"
            % ",".join("?" * len(tracked)), sorted(tracked)).fetchall() if tracked else []
        srv = self.conn.execute("SELECT name FROM servers ORDER BY id LIMIT 1").fetchone()
        from ..services import arr as arr_svc
        sab = arr_svc.configured(self.conn).get("sabnzbd", False)
        for topic, payload in discovery(self._cfg, users, srv["name"] if srv else None, sab=sab).items():
            self._publish(topic, payload)
        if not sab:
            for topic in sab_topics(self._cfg):
                if self._sent.get(topic):            # was published, SABnzbd since disconnected: remove from HA
                    self._publish(topic, "", force=True)
                    self._sent[topic] = ""
        published = set(settings.get(self.conn, "mqtt.published_users"))
        live = {u["id"] for u in users}
        for uid in published - live:
            for topic in removed_user_topics(self._cfg, uid):
                self._publish(topic, "", force=True)
                self._sent.pop(topic, None)
        if published != live:
            settings.set(self.conn, "mqtt.published_users", sorted(live))

        base = self._cfg["mqtt.base_topic"]
        active = activity.list_active(self.conn)
        self._publish(f"{base}/state", json.dumps(server_state(self.conn, active, now), sort_keys=True), force)
        self._publish(f"{base}/sessions", json.dumps({"sessions": [_session_attrs(a) for a in active]}), force)
        for uid in sorted(live):
            self._publish(f"{base}/user/{uid}", json.dumps(user_state(uid, active), sort_keys=True), force)

    def _drain_commands(self, tracked: set[int] | None) -> None:
        while True:
            try:
                uid = self.commands.get_nowait()
            except queue.Empty:
                return
            if tracked is None or uid not in tracked:
                log.warning("ignoring stop command for user %s (not tracked)", uid)
                continue
            try:
                n = self.stop_user(uid)
                log.info("HA stop button: stopped %d stream(s) for user %s", n, uid)
            except Exception as e:  # noqa: BLE001 - a Plex error mustn't take the loop down
                log.warning("HA stop button for user %s failed: %s", uid, e)
