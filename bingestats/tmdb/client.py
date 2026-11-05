"""TMDB's API v3. GET only: Binge Stats reads production status, never writes.

Either kind of credential from themoviedb.org → Settings → API works: the
"API Key" (32 hex characters, sent as ?api_key=) or the much longer "API Read
Access Token" (a JWT, sent as a bearer token).
"""
from __future__ import annotations

import time
from typing import Any

import requests

from .. import __version__

BASE = "https://api.themoviedb.org/3"


class TmdbError(Exception):
    pass


class TmdbClient:
    def __init__(self, key: str, *, timeout: float = 15, http: requests.Session | None = None,
                 sleep=time.sleep, max_retries: int = 3):
        self.key = (key or "").strip()
        self.timeout = timeout
        self.http = http or requests.Session()
        self.sleep = sleep
        self.max_retries = max_retries

    def _auth(self) -> tuple[dict, dict]:
        if self.key.startswith("eyJ"):                       # a read access token (JWT)
            return {"Authorization": f"Bearer {self.key}"}, {}
        return {}, {"api_key": self.key}

    def get(self, path: str, **params) -> Any:
        """One GET. Returns None for a 404 (TMDB dropped the id). Waits and retries on 429."""
        headers, auth = self._auth()
        headers.update({"Accept": "application/json", "User-Agent": f"BingeStats/{__version__}"})
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.get(f"{BASE}/{path.lstrip('/')}", params={**params, **auth}, headers=headers,
                                  timeout=self.timeout)
            except requests.RequestException as e:
                raise TmdbError(f"Can't reach TMDB: {e.__class__.__name__}") from e
            if r.status_code == 429 and attempt < self.max_retries:
                try:
                    wait = float(r.headers.get("Retry-After") or 1)
                except ValueError:
                    wait = 1.0
                self.sleep(min(max(wait, 0.5), 10))
                continue
            break
        if r.status_code == 401:
            raise TmdbError("TMDB refused the key")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise TmdbError(f"TMDB {path}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise TmdbError(f"TMDB {path}: not JSON") from e

    def tv(self, tmdb_id: int) -> dict | None:
        return self.get(f"tv/{int(tmdb_id)}")
