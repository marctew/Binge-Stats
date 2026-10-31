# Binge Stats

Self-hosted Plex monitoring and statistics. A modern take on Tautulli: live activity, full history, stats and graphs, notifications through Apprise, Home Assistant over MQTT, plus binge and completion tracking, transcode insights, library health and a shareable year in review.

See [DESIGN.md](DESIGN.md) for the decisions, [SPEC.md](SPEC.md) for the build spec, and [docs/verification.md](docs/verification.md) for what's been checked against a real server.

**Status:** milestone 1 (live ingest, Activity, History, setup wizard). Milestone 2, the Tautulli import, is next.

## Run it

```bash
git clone https://github.com/marctew/Binge-Stats && cd Binge-Stats
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
```

Open `http://<host>:8181`. Create the admin login, then **Sign in with Plex** (or paste a URL and token). Data lives in `./data`.

To update:

```bash
git pull && GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
scripts/build-css.sh                      # fetches pinned htmx/Alpine/Tailwind, builds CSS
DATA_DIR=./data .venv/bin/python -m bingestats web --dev
DATA_DIR=./data .venv/bin/python -m bingestats worker  # second shell
.venv/bin/python -m pytest
```
