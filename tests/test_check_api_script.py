"""scripts/check_api.py (Design Uniformity's checker, carried by the design updater) against a running Binge."""
import subprocess
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from bingestats.services import api_tokens
from tests.test_api_feeds import FakePhoto, add_item
from tests.test_api_standard import healthy
from tests.test_pins import NOW

SCRIPT = Path(__file__).parent.parent / "scripts" / "check_api.py"


@pytest.fixture
def served(app, app_conn, monkeypatch):
    from bingestats.services import activity
    monkeypatch.setattr(activity, "plex_client", lambda conn: FakePhoto())
    healthy(app_conn)
    add_item(app_conn, 600, "movie", "Rumours", NOW, year=1977, thumb="/library/metadata/600/thumb/1")
    app_conn.commit()
    app.extensions["du_api"].stopping.set()          # the events check reads one line; don't hold the stream open
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def run(*args):
    return subprocess.run(["python3", str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


def test_passes_against_binge(served, app_conn):
    r = run(served, api_tokens.create(app_conn, "check", "read"), api_tokens.create(app_conn, "check-w", "write"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All good." in r.stdout and "FAIL" not in r.stdout
    assert "a write token in the URL gets 400" in r.stdout and "feed arrivals" in r.stdout


def test_fails_with_a_bad_token(served):
    r = run(served, "bs_nope")
    assert r.returncode == 1 and "FAIL  meta answers 200 JSON" in r.stdout


def test_usage():
    r = run()
    assert r.returncode == 2 and "Usage" in r.stderr
