"""GET-only HTTP clients for Sonarr/Radarr (API v3) and SABnzbd."""
from __future__ import annotations

from typing import Any

import requests

from .. import __version__


class ArrError(Exception):
    pass


def _base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


class ArrClient:
    """Sonarr or Radarr, API v3. GET only."""

    def __init__(self, url: str, api_key: str, *, timeout: float = 30, http: requests.Session | None = None):
        self.url = _base(url)
        self.api_key = api_key
        self.timeout = timeout
        self.http = http or requests.Session()

    def get(self, path: str, **params) -> Any:
        try:
            r = self.http.get(f"{self.url}/api/v3/{path.lstrip('/')}", params=params,
                              headers={"X-Api-Key": self.api_key, "Accept": "application/json",
                                       "User-Agent": f"BingeStats/{__version__}"}, timeout=self.timeout)
        except requests.RequestException as e:
            raise ArrError(f"Can't reach {self.url}: {e.__class__.__name__}") from e
        if r.status_code == 401:
            raise ArrError("The API key was refused")
        if r.status_code >= 400:
            raise ArrError(f"{path}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise ArrError(f"{path}: not JSON (is the URL right?)") from e

    def paged(self, path: str, page_size: int = 1000, max_pages: int = 100, **params) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            d = self.get(path, page=page, pageSize=page_size, **params)
            records = d.get("records") or []
            out.extend(records)
            if len(out) >= (d.get("totalRecords") or 0) or not records:
                break
        return out


class SabClient:
    """SABnzbd's API. Only read modes are used (version, server_stats, history, queue)."""

    READ_MODES = {"version", "server_stats", "history", "queue"}

    def __init__(self, url: str, api_key: str, *, timeout: float = 20, http: requests.Session | None = None):
        self.url = _base(url)
        self.api_key = api_key
        self.timeout = timeout
        self.http = http or requests.Session()

    def get(self, mode: str, *, timeout: float | None = None, **params) -> Any:
        if mode not in self.READ_MODES:
            raise ArrError(f"SABnzbd mode {mode} isn't allowed (read-only)")
        try:
            r = self.http.get(f"{self.url}/api", params={"mode": mode, "output": "json", "apikey": self.api_key, **params},
                              timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise ArrError(f"Can't reach {self.url}: {e.__class__.__name__}") from e
        if r.status_code >= 400:
            raise ArrError(f"SABnzbd {mode}: HTTP {r.status_code}")
        try:
            d = r.json()
        except ValueError as e:
            raise ArrError("SABnzbd didn't answer with JSON (is the URL right?)") from e
        if isinstance(d, dict) and d.get("status") is False:
            raise ArrError(f"SABnzbd: {d.get('error') or 'refused'}")
        return d
