import copy
import json
import re
from pathlib import Path

import pytest

from bingestats import create_app, db
from bingestats.plex.client import PlexError
from bingestats.services import auth, settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / "plex" / name).read_text())


class Clock:
    def __init__(self, t: float = 1_800_000_000):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakePlex:
    """Stands in for PlexClient in poller tests."""

    def __init__(self):
        self.sessions_data = load_fixture("sessions_empty.json")
        self.metadata_data: dict[int, dict] = {5001: load_fixture("metadata_5001.json")}
        self.fail = False
        self.metadata_calls = 0
        self.terminated: list[tuple[str, str]] = []

    def identity(self):
        if self.fail:
            raise PlexError("down")
        return load_fixture("identity.json")

    def root(self):
        return load_fixture("root.json")

    def sessions(self):
        if self.fail:
            raise PlexError("cannot connect")
        return copy.deepcopy(self.sessions_data)

    def metadata(self, rating_key):
        self.metadata_calls += 1
        if int(rating_key) not in self.metadata_data:
            raise PlexError("HTTP 404")
        return copy.deepcopy(self.metadata_data[int(rating_key)])

    def terminate(self, session_id, reason):
        self.terminated.append((session_id, reason))

    # helpers for tests
    def play(self, *names_or_dicts):
        metas = []
        for n in names_or_dicts:
            d = load_fixture(n) if isinstance(n, str) else n
            metas.extend(d["MediaContainer"]["Metadata"])
        self.sessions_data = {"MediaContainer": {"size": len(metas), "Metadata": metas}}

    def stop_all(self):
        self.sessions_data = load_fixture("sessions_empty.json")

    def meta(self, index=0):
        return self.sessions_data["MediaContainer"]["Metadata"][index]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def plex():
    return FakePlex()


@pytest.fixture
def app(tmp_path):
    app = create_app(str(tmp_path / "data"), testing=True)
    return app


@pytest.fixture
def app_conn(app):
    c = db.connect(app.config["BS_CONFIG"].db_path)
    yield c
    c.close()


class Browser:
    """Test client that tracks the CSRF token like a real page would."""

    def __init__(self, client):
        self.client = client
        self.csrf = None

    def get(self, url, **kw):
        r = self.client.get(url, **kw)
        m = re.search(rb'name="csrf_token" value="([^"]+)"', r.data) or \
            re.search(rb'"X-CSRF-Token": "([^"]+)"', r.data)
        if m:
            self.csrf = m.group(1).decode()
        return r

    def post(self, url, data=None, headers=None, **kw):
        if self.csrf is None:
            self.get("/login")
        data = dict(data or {})
        data.setdefault("csrf_token", self.csrf)
        return self.client.post(url, data=data, headers=headers or {}, **kw)


@pytest.fixture
def browser(app):
    return Browser(app.test_client())


@pytest.fixture
def logged_in(app, app_conn, browser):
    auth.set_admin(app_conn, "marc", "correct horse")
    settings.set(app_conn, "plex.url", "http://plex.test:32400")
    settings.set(app_conn, "plex.token", "tok")
    browser.get("/login")
    r = browser.post("/login", {"username": "marc", "password": "correct horse"})
    assert r.status_code == 302
    browser.get("/settings")  # login rotates the session, so pick up the new CSRF token
    return browser
