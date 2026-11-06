"""The API standard's actions (section 7): POST /api/v1/actions/<name> with a JSON body.

Each action is one named thing to do, listed in /api/v1/meta with its body. Nothing destructive is
offered (no stopping streams) unless Marc asks for that one. Every call, refused or not, goes in
the action log (services/api_audit.py). A repeat with the same Idempotency-Key within 24 hours,
from the same token, gets the first answer back and does nothing twice.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import alerts, api_audit, arr, pin_api, pins

IDEMPOTENCY_S = 24 * 3600
KEY_MAX = 100


class ActionError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _bad(message: str) -> ActionError:
    return ActionError("bad_request", message, 400)


def _int(body: dict, name: str, required: bool = True) -> int | None:
    v = body.get(name)
    if v is None:
        if required:
            raise _bad(f"Send {name}, a whole number.")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).lstrip("-").isdigit():
        raise _bad(f"{name} must be a whole number.")
    return int(v)


def _str(body: dict, name: str, required: bool = True) -> str | None:
    v = body.get(name)
    if v in (None, ""):
        if required:
            raise _bad(f"Send {name}.")
        return None
    if not isinstance(v, str):
        raise _bad(f"{name} must be text.")
    return v


@dataclass
class Action:
    name: str
    title: str
    body: dict                     # documented in meta: field -> type
    run: Callable[[sqlite3.Connection, dict], tuple[str, dict]]   # -> (message, result)
    when: Callable[[sqlite3.Connection], bool] | None = None
    off_message: str = ""


ACTIONS: dict[str, Action] = {}


def _action(name: str, title: str, body: dict, when=None, off_message: str = ""):
    def deco(fn):
        ACTIONS[name] = Action(name, title, body, fn, when, off_message)
        return fn
    return deco


# -- pins ---------------------------------------------------------------------------------------

def _pin_target(conn, body) -> tuple[int, int, str]:
    tvdb_id = _int(body, "tvdb_id")
    uid = pin_api.resolve_user(conn, _int(body, "user_id", required=False))
    if uid is None:
        raise ActionError("not_found", "There's no such user, and no Plex owner to default to.", 404)
    show = pins.show(conn, tvdb_id)
    if show is None:
        raise ActionError("not_found", f"There's no show with TVDB id {tvdb_id} in Plex or Sonarr.", 404)
    return uid, tvdb_id, show.get("title") or f"TVDB {tvdb_id}"


@_action("pin_show", "Pin a show", {"tvdb_id": "int", "user_id": "int, optional (default: the Plex owner)"})
def _pin_show(conn, body):
    uid, tvdb_id, title = _pin_target(conn, body)
    if tvdb_id in pins.pinned(conn, uid):
        return f"{title} was already pinned.", {"tvdb_id": tvdb_id, "user_id": uid, "pinned": True, "changed": False}
    pins.toggle(conn, uid, tvdb_id)
    return f"Pinned {title}.", {"tvdb_id": tvdb_id, "user_id": uid, "pinned": True, "changed": True}


@_action("unpin_show", "Unpin a show", {"tvdb_id": "int", "user_id": "int, optional (default: the Plex owner)"})
def _unpin_show(conn, body):
    uid, tvdb_id, title = _pin_target(conn, body)
    changed = pins.unpin(conn, uid, tvdb_id)
    return (f"Unpinned {title}." if changed else f"{title} wasn't pinned."), \
        {"tvdb_id": tvdb_id, "user_id": uid, "pinned": False, "changed": changed}


# -- alerts ---------------------------------------------------------------------------------------

@_action("ack_alert", "Clear an alert", {"alert_id": "int"})
def _ack_alert(conn, body):
    alert_id = _int(body, "alert_id")
    if not alerts.acknowledge(conn, alert_id):
        raise ActionError("not_found", f"There's no alert {alert_id}.", 404)
    return "Alert cleared.", {"alert_id": alert_id}


@_action("ack_all_alerts", "Clear every alert", {})
def _ack_all(conn, body):
    n = alerts.acknowledge(conn)
    return (f"Cleared {n} alert{'s' if n != 1 else ''}." if n else "No alerts to clear."), {"acknowledged": n}


# -- downloads --------------------------------------------------------------------------------------

def _job_title(conn, nzo_id: str) -> str:
    q = arr.download_queue(conn) or {}
    for j in q.get("items") or []:
        if j.get("nzo_id") == nzo_id:
            return " ".join(x for x in (j.get("title"), (j.get("sub") or "").split(" · ")[0]) if x)
    return "that job"


@_action("move_download", "Move a download in SABnzbd's queue",
         {"nzo_id": "str", "to": "top | bottom, or give above / below", "above": "str, optional", "below": "str, optional"},
         when=arr.moves_allowed, off_message="Moving downloads is turned off in Settings → SABnzbd.")
def _move_download(conn, body):
    nzo_id = _str(body, "nzo_id")
    to, above, below = _str(body, "to", False), _str(body, "above", False), _str(body, "below", False)
    title = _job_title(conn, nzo_id)
    res = arr.move(conn, nzo_id, to=to, above=above, below=below)
    if not res.get("moved"):
        return f"{title[:1].upper() + title[1:]} is already there.", res
    where = {"top": " to the top", "bottom": " to the bottom"}.get(to or "", "")
    return f"Moved {title}{where}.", res


# -- running one -----------------------------------------------------------------------------------

def listed(conn: sqlite3.Connection) -> list[Action]:
    return [a for a in ACTIONS.values() if a.when is None or a.when(conn)]


def _answer_error(code: str, message: str, status: int) -> tuple[int, dict]:
    return status, {"error": message, "code": code}


def run(conn: sqlite3.Connection, name: str, body: Any, *, token_id: int | None, token_name: str,
        key: str | None = None, now: float | None = None) -> tuple[int, dict]:
    """Do action `name` once. Returns (HTTP status, JSON body) in the standard's shapes."""
    now = now or time.time()
    who = token_id or 0                      # the admin session shares one idempotency space
    if key is not None and not (0 < len(key) <= KEY_MAX):
        return _answer_error("bad_request", f"Idempotency-Key must be 1 to {KEY_MAX} characters.", 400)
    if key is not None:
        row = conn.execute("SELECT action, status, body, created_at FROM api_idempotency WHERE token_id = ? AND key = ?",
                           (who, key)).fetchone()
        if row is not None and row["created_at"] >= now - IDEMPOTENCY_S:
            if row["action"] != name:
                return _answer_error("bad_request", "That Idempotency-Key was already used for a different action.", 400)
            return row["status"], json.loads(row["body"])

    action = ACTIONS.get(name)
    if action is None:
        status, out = _answer_error("not_found", f"There's no action called '{name}'. Actions: "
                                    + ", ".join(a.name for a in listed(conn)) + ".", 404)
    elif action.when is not None and not action.when(conn):
        status, out = _answer_error("forbidden", action.off_message or "That action is turned off.", 403)
    elif not isinstance(body, dict):
        status, out = _answer_error("bad_request", "Send a JSON object as the body.", 400)
    else:
        from ..arr.client import ArrError
        from ..plex.client import PlexError
        try:
            message, result = action.run(conn, body)
            status, out = 200, {"ok": True, "message": message, "result": result}
        except ActionError as e:
            status, out = _answer_error(e.code, e.message, e.status)
        except PermissionError as e:
            status, out = _answer_error("forbidden", str(e), 403)
        except LookupError as e:
            status, out = _answer_error("not_found", str(e), 404)
        except ValueError as e:
            status, out = _answer_error("bad_request", str(e), 400)
        except (ArrError, PlexError) as e:
            status, out = _answer_error("upstream", f"That didn't work upstream: {e}", 502)

    api_audit.record(conn, token_id=token_id, token_name=token_name, action=name,
                     body=body if isinstance(body, dict) else None, status=status,
                     message=out.get("message") or out.get("error"), now=now)
    if key is not None and status < 500:     # an upstream failure may be retried with the same key
        conn.execute("INSERT OR REPLACE INTO api_idempotency (token_id, key, action, status, body, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (who, key, name, status, json.dumps(out), int(now)))
    return status, out


def prune(conn: sqlite3.Connection, now: float | None = None) -> int:
    return conn.execute("DELETE FROM api_idempotency WHERE created_at < ?",
                        (int((now or time.time()) - IDEMPOTENCY_S),)).rowcount
