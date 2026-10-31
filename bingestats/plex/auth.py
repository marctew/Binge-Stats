"""plex.tv PIN sign-in, used by the setup wizard to get a token without pasting one."""
from __future__ import annotations

from urllib.parse import urlencode

import requests

from .client import PRODUCT, PlexError, plex_headers

PLEX_TV = "https://plex.tv"


def create_pin(client_id: str) -> dict:
    r = requests.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "true"},
                      headers=plex_headers(client_id), timeout=10)
    if r.status_code >= 400:
        raise PlexError(f"plex.tv pin: HTTP {r.status_code}")
    d = r.json()
    return {"id": d["id"], "code": d["code"]}


def auth_url(client_id: str, code: str, forward_url: str | None = None) -> str:
    params = {
        "clientID": client_id,
        "code": code,
        "context[device][product]": PRODUCT,
    }
    if forward_url:
        params["forwardUrl"] = forward_url
    return "https://app.plex.tv/auth#?" + urlencode(params)


def check_pin(client_id: str, pin_id: int) -> str | None:
    """Returns the token once the user has approved the PIN, else None."""
    r = requests.get(f"{PLEX_TV}/api/v2/pins/{int(pin_id)}", headers=plex_headers(client_id), timeout=10)
    if r.status_code == 404:
        raise PlexError("PIN expired, start again")
    if r.status_code >= 400:
        raise PlexError(f"plex.tv pin: HTTP {r.status_code}")
    return r.json().get("authToken") or None


def list_servers(client_id: str, token: str) -> list[dict]:
    """Servers this account can reach, with their connection URLs (local first)."""
    r = requests.get(f"{PLEX_TV}/api/v2/resources", params={"includeHttps": 1},
                     headers=plex_headers(client_id, token), timeout=10)
    if r.status_code >= 400:
        raise PlexError(f"plex.tv resources: HTTP {r.status_code}")
    out = []
    for res in r.json():
        if "server" not in (res.get("provides") or ""):
            continue
        conns = sorted(res.get("connections") or [],
                       key=lambda c: (not c.get("local"), c.get("relay", False), c.get("protocol") != "http"))
        urls = []
        for c in conns:
            if c.get("relay"):
                continue
            if c.get("local"):
                urls.append(f"http://{c['address']}:{c['port']}")
            urls.append(c["uri"])
        out.append({"name": res.get("name"), "owned": bool(res.get("owned")),
                    "machine_id": res.get("clientIdentifier"), "urls": list(dict.fromkeys(urls))})
    return out
