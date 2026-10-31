"""Plex notification websocket. Only used to nudge the poller: the poll is
the source of truth, so a dropped socket costs latency, never data."""
from __future__ import annotations

import json
import logging
import threading
import time

import websocket

from .client import plex_headers

log = logging.getLogger(__name__)


class PlexNudger(threading.Thread):
    def __init__(self, ws_url: str, token: str, client_id: str, nudge: threading.Event,
                 stop: threading.Event):
        super().__init__(name="plex-ws", daemon=True)
        self.ws_url = ws_url
        self.token = token
        self.client_id = client_id
        self.nudge = nudge
        self.stop_event = stop
        self.connected = False
        self._ws: websocket.WebSocketApp | None = None

    def _on_message(self, _ws, message: str) -> None:
        try:
            data = json.loads(message)
        except ValueError:
            return
        container = data.get("NotificationContainer") or {}
        if container.get("type") == "playing" or container.get("PlaySessionStateNotification"):
            self.nudge.set()

    def _on_open(self, _ws) -> None:
        self.connected = True
        log.info("Plex websocket connected")

    def _on_close(self, *_a) -> None:
        self.connected = False

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> None:
        backoff = 2
        while not self.stop_event.is_set():
            headers = [f"{k}: {v}" for k, v in plex_headers(self.client_id, self.token).items()]
            self._ws = websocket.WebSocketApp(
                self.ws_url, header=headers, on_message=self._on_message,
                on_open=self._on_open, on_close=self._on_close,
            )
            started = time.monotonic()
            try:
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:  # noqa: BLE001
                log.debug("websocket error: %s", e)
            self.connected = False
            if self.stop_event.is_set():
                break
            if time.monotonic() - started > 60:
                backoff = 2
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, 120)
