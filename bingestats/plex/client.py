"""HTTP client for a Plex Media Server and the bits of plex.tv we need."""
from __future__ import annotations

import logging
from typing import Any

import requests

from .. import __version__

log = logging.getLogger(__name__)

PRODUCT = "Binge Stats"


class PlexError(Exception):
    pass


class PlexAuthError(PlexError):
    pass


def plex_headers(client_id: str, token: str | None = None) -> dict[str, str]:
    h = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": __version__,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Platform": "Web",
        "X-Plex-Device-Name": PRODUCT,
    }
    if token:
        h["X-Plex-Token"] = token
    return h


class PlexClient:
    def __init__(self, url: str, token: str, client_id: str, verify_tls: bool = True,
                 timeout: float = 10.0, session: requests.Session | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self.client_id = client_id
        self.verify = verify_tls
        self.timeout = timeout
        self.http = session or requests.Session()

    def _request(self, path: str, params: dict | None = None, *, raw: bool = False,
                 timeout: float | None = None) -> Any:
        url = f"{self.url}{path}"
        last: Exception | None = None
        for attempt in range(2):
            try:
                r = self.http.get(url, params=params, headers=plex_headers(self.client_id, self.token),
                                  timeout=timeout or self.timeout, verify=self.verify)
            except requests.ConnectionError as e:
                last = e
                continue
            except requests.RequestException as e:
                raise PlexError(f"{path}: {e.__class__.__name__}") from e
            if r.status_code == 401:
                raise PlexAuthError("Plex rejected the token (401)")
            if r.status_code >= 400:
                raise PlexError(f"{path}: HTTP {r.status_code}")
            if raw:
                return r
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError as e:
                raise PlexError(f"{path}: response was not JSON") from e
        raise PlexError(f"{path}: cannot connect ({last.__class__.__name__ if last else 'error'})")

    def identity(self) -> dict:
        return self._request("/identity")

    def root(self) -> dict:
        return self._request("/")

    def sessions(self) -> dict:
        return self._request("/status/sessions")

    def metadata(self, rating_key: int) -> dict:
        return self._request(f"/library/metadata/{int(rating_key)}")

    def sections(self) -> dict:
        return self._request("/library/sections")

    # Tag elements we never use; newer PMS drops them, older ones ignore the param.
    LEAN = "Role,Director,Writer,Producer,Genre,Country,Image,UltraBlurColors,Guid,Rating,Collection,Label"

    def section_items(self, section_id: int, plex_type: int, start: int, size: int) -> dict:
        return self._request(f"/library/sections/{int(section_id)}/all", {
            "type": plex_type, "X-Plex-Container-Start": start, "X-Plex-Container-Size": size,
            "excludeElements": self.LEAN,
        }, timeout=60)

    # -- plex.tv (account-level, uses the same token) --------------------------

    def plextv(self, path: str, accept: str = "application/json"):
        headers = plex_headers(self.client_id, self.token)
        headers["Accept"] = accept
        try:
            r = self.http.get(f"https://plex.tv{path}", headers=headers, timeout=15)
        except requests.RequestException as e:
            raise PlexError(f"plex.tv{path}: {e.__class__.__name__}") from e
        if r.status_code == 401:
            raise PlexAuthError("plex.tv rejected the token (401)")
        if r.status_code >= 400:
            raise PlexError(f"plex.tv{path}: HTTP {r.status_code}")
        return r.json() if accept == "application/json" else r.text

    def terminate(self, session_id: str, reason: str) -> None:
        self._request("/status/sessions/terminate",
                      {"sessionId": session_id, "reason": reason or "Stream stopped"})

    def photo(self, path: str, width: int, height: int) -> requests.Response:
        """Resized image through Plex's photo transcoder."""
        params = {"width": width, "height": height, "minSize": 1, "upscale": 0,
                  "url": path, "format": "jpeg"}
        return self._request("/photo/:/transcode", params, raw=True, timeout=20)

    def websocket_url(self) -> str:
        base = self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/:/websockets/notifications"


def fetch_external_image(url: str, timeout: float = 10.0) -> requests.Response:
    """For plex.tv avatars, which live outside the server."""
    if not url.startswith("https://"):
        raise PlexError("refusing non-https image url")
    host = url.split("/", 3)[2].lower()
    if not (host == "plex.tv" or host.endswith(".plex.tv") or host.endswith(".plex.direct")):
        raise PlexError("refusing image from unknown host")
    r = requests.get(url, timeout=timeout)
    if r.status_code >= 400:
        raise PlexError(f"image: HTTP {r.status_code}")
    return r
