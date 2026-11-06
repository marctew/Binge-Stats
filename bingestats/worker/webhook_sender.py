"""Sends webhook deliveries (services/webhooks.py) once a second, on its own connection, so a slow
Home Assistant never holds up a Plex poll."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .. import db
from ..services import webhooks

log = logging.getLogger("bingestats.webhooks")
TICK_S = 1.0


class WebhookSender(threading.Thread):
    def __init__(self, db_path: Path, stop: threading.Event):
        super().__init__(name="webhooks", daemon=True)
        self.db_path, self.stop_event = db_path, stop

    def run(self) -> None:
        conn = db.connect(self.db_path)
        while not self.stop_event.wait(TICK_S):
            try:
                webhooks.deliver_due(conn)
            except Exception:  # noqa: BLE001 - never take the worker down over a webhook
                log.exception("webhook delivery failed")
        conn.close()
