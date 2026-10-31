"""Connecting Binge Stats to a Plex server (setup wizard and Settings)."""
from __future__ import annotations

import sqlite3
import uuid

from ..plex import auth as plex_auth
from ..plex.client import PlexClient, PlexError
from ..plex.parse import parse_identity
from . import settings


def client_id(conn: sqlite3.Connection) -> str:
    cid = settings.get(conn, "plex.client_id")
    if not cid:
        cid = str(uuid.uuid4())
        settings.set(conn, "plex.client_id", cid)
    return cid


def normalise_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Plex URL is required")
    if "://" not in url:
        url = "http://" + url
    if not url.startswith(("http://", "https://")):
        raise ValueError("Plex URL must start with http:// or https://")
    host = url.split("://", 1)[1]
    if ":" not in host.split("/", 1)[0] and not url.startswith("https://"):
        url += ":32400"
    return url


def test_connection(conn: sqlite3.Connection, url: str, token: str, verify_tls: bool = True) -> dict:
    """Raises PlexError with a readable message if it doesn't work."""
    client = PlexClient(url, token, client_id(conn), verify_tls=verify_tls, timeout=8)
    ident = parse_identity(client.identity(), client.root())
    if not ident.machine_id or ident.machine_id == "None":
        raise PlexError("That URL answered but doesn't look like a Plex server")
    client.sessions()  # proves the token can see sessions (owner/admin token)
    return {"name": ident.name, "version": ident.version, "machine_id": ident.machine_id}


def save_connection(conn: sqlite3.Connection, url: str, token: str, verify_tls: bool = True) -> dict:
    url = normalise_url(url)
    info = test_connection(conn, url, token, verify_tls)
    settings.set(conn, "plex.url", url)
    settings.set(conn, "plex.token", token)
    settings.set(conn, "plex.verify_tls", verify_tls)
    settings.set(conn, "plex.pending_token", "")
    return info


def start_pin(conn: sqlite3.Connection, forward_url: str | None = None) -> dict:
    cid = client_id(conn)
    pin = plex_auth.create_pin(cid)
    return {"id": pin["id"], "url": plex_auth.auth_url(cid, pin["code"], forward_url)}


def check_pin(conn: sqlite3.Connection, pin_id: int) -> list[dict] | None:
    """None while waiting. Once approved, stores the token as pending and
    returns the servers the account can see."""
    cid = client_id(conn)
    token = plex_auth.check_pin(cid, pin_id)
    if not token:
        return None
    settings.set(conn, "plex.pending_token", token)
    try:
        return plex_auth.list_servers(cid, token)
    except PlexError:
        return []


def save_from_pending(conn: sqlite3.Connection, url: str, verify_tls: bool = True) -> dict:
    token = settings.get(conn, "plex.pending_token")
    if not token:
        raise PlexError("Sign in with Plex first")
    return save_connection(conn, url, token, verify_tls)
