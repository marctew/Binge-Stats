"""HTTP clients for Sonarr/Radarr (API v3) and SABnzbd. GET only, with one exception:
SabClient.move (SABnzbd's mode=switch), used only while sabnzbd.allow_moves is on."""
from __future__ import annotations

import re
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


# SABnzbd 5 uses a UUID ("61fb3d9a-5559-..."); older versions "SABnzbd_nzo_abc123".
NZO_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class SabClient:
    """SABnzbd's API. Read modes only (version, server_stats, history, queue), plus move()."""

    READ_MODES = {"version", "server_stats", "history", "queue"}
    WRITE_MODES = {"switch"}

    def __init__(self, url: str, api_key: str, *, timeout: float = 20, http: requests.Session | None = None):
        self.url = _base(url)
        self.api_key = api_key
        self.timeout = timeout
        self.http = http or requests.Session()

    def get(self, mode: str, *, timeout: float | None = None, **params) -> Any:
        if mode not in self.READ_MODES:
            raise ArrError(f"SABnzbd mode {mode} isn't allowed (read-only)")
        return self._call(mode, timeout, params)

    def move(self, nzo_id: str, target_nzo_id: str, *, allowed: bool) -> dict:
        """Move a job to where another job is: just before it when moving up, just after it when
        moving down (SABnzbd takes the job out, then puts it at the target's old index). The job
        takes the target's priority. Returns where it really ended up: {position, priority}."""
        if not allowed:
            raise ArrError("Moving jobs is turned off in Settings")
        if not (NZO_ID.match(nzo_id or "") and NZO_ID.match(target_nzo_id or "")):
            raise ArrError("Not a SABnzbd job id")
        d = self._call("switch", 15, {"value": nzo_id, "value2": target_nzo_id})
        res = d.get("result") if isinstance(d, dict) else None
        if not isinstance(res, dict) or int(res.get("position", -1)) < 0:
            raise ArrError("SABnzbd didn't move the job (has it finished?)")
        return {"position": int(res["position"]), "priority": int(res.get("priority") or 0)}

    def _call(self, mode: str, timeout: float | None, params: dict) -> Any:
        if mode not in self.READ_MODES | self.WRITE_MODES:
            raise ArrError(f"SABnzbd mode {mode} isn't allowed")
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
