# Binge Stats

Self-hosted Plex monitoring and statistics. A modern take on Tautulli: live activity, full history, stats and graphs, notifications through Apprise, Home Assistant over MQTT, plus binge and completion tracking, transcode insights, library health and a shareable year in review.

See [DESIGN.md](DESIGN.md) for the decisions, [SPEC.md](SPEC.md) for the build spec, and [docs/verification.md](docs/verification.md) for what's been checked against a real server.

**Status:** milestone 6 (insights: binges, series completion, abandoned shows, transcode analysis with re-encode candidates, library health with CSV; on top of Home Assistant over MQTT, stream limits, account-sharing alerts, notifications, the dashboard, graphs, locations, REST API, live ingest, history, users, libraries and the Tautulli import). Milestone 7, Wrapped (a shareable year in review), is next.

## Run it

```bash
git clone https://github.com/marctew/Binge-Stats && cd Binge-Stats
sudo GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
```

Open `http://<host>:8181`. Create the admin login, then **Sign in with Plex** (or paste a URL and token). Data lives in `./data`.

To update, run `scripts/update.sh` from anywhere, as root or as the repo's owner. It pulls as the owner, rebuilds, and waits for the health check. To make it a one-word command:

```bash
sudo ln -sf /opt/binge-stats/scripts/update.sh /usr/local/bin/binge-update
```

## Importing from Tautulli

Copy `tautulli.db` into `data/import/` (or mount Tautulli's data folder read-only and use that path), then use **Settings → Import from Tautulli**. Check first, then import. It's safe to run again later to pick up plays Tautulli recorded in the meantime; nothing is imported twice, and plays Binge Stats already recorded live are skipped. The file holds your users' Plex tokens, so delete the copy when you're done.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
scripts/build-css.sh                      # fetches pinned htmx/Alpine/Tailwind, builds CSS
DATA_DIR=./data .venv/bin/python -m bingestats web --dev
DATA_DIR=./data .venv/bin/python -m bingestats worker  # second shell
.venv/bin/python -m pytest
```
