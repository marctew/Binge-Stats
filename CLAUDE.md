# Binge Stats

Self-hosted Plex monitoring and stats, a modern Tautulli replacement. Read DESIGN.md before changing behaviour (it records decisions already made) and SPEC.md for build detail. Build milestone by milestone (SPEC.md section 19).

## Layout

- `bingestats/` the app: Flask + plain `sqlite3` (schema and migrations in `bingestats/db.py`), Jinja + HTMX + Alpine + Tailwind, ECharts. No ORM, no JS framework, no Node.
- `bingestats/plex/` the only code that knows Plex's API shape. Everything else uses its dataclasses.
- `bingestats/arr/` the only code that knows Sonarr, Radarr and SABnzbd's APIs. GET only, except `SabClient.move` (SABnzbd `mode=switch`, behind `sabnzbd.allow_moves`). Never add another write call.
- `bingestats/tmdb/` the only code that knows TMDB's API. GET only.
- `bingestats/worker/` the only process that writes sessions, plays, library and user data. The web process writes settings, rules, tokens, jobs, alert acknowledgements, viewer logins, share salts and pins only.
- `bingestats/services/` all business operations. UI routes and the REST API both call these; routes stay thin.
- `docker/` Dockerfile and compose. `scripts/build-css.sh` for local CSS builds.

## Rules

- Plex only. SQLite only. Don't add Jellyfin/Emby or another database.
- One admin, plus viewer logins that see only their own data (`web/routes_me.py`, gated in `web/auth.py`). Viewer access is an allow-list: any new page is admin-only unless it's added to the `me` blueprint and scoped to `session["viewer"]`.
- Ingest is polling `/status/sessions`; the websocket only nudges a poll. No Plex webhooks.
- The Plex token never reaches the browser: images go through `/img/`.
- All timestamps are UTC epoch seconds; display in the `ui.timezone` setting.
- Long work runs as a row in `jobs`, picked up by the worker. Never do it in a request.
- Settings live in the `settings` table, read only through `services/settings.py`.
- UI follows the Design Uniformity spec (`C:/ClaudeCowork/Design Uniformity/DESIGN-SPEC.md` on Marc's PC). Its Tailwind preset and components (`bingestats/static/css/design/`), `design-card.js`, the design card partial, `du_flask.py` and `docs/design/` are copied in and committed by the design updater (`/etc/design-uniformity/binge-stats.toml` on the host): don't edit them here; change Design Uniformity and release it. `input.css` holds only Binge-specific additions.
- Built to `docs/design/APP-STANDARD.md`, as its exception: Binge stays in Docker and on Tailwind (section 1), but has `/healthz` (no login), `scripts/update.sh` (pull, rebuild, wait for health) and the Design system card in Settings.
- Reuse the Jinja component macros and the shared ECharts theme; no page-specific one-offs. Every chart and list needs an empty state. Pages must work at phone width.
- Front-end assets are pinned (version + sha256) in `bingestats/static/vendor/VERSIONS` and fetched at build time by `scripts/fetch-assets.sh`. No CDNs at runtime.
- Runtime data (`/data`, `*.db`, caches, config with secrets) stays out of the tracked tree.
- v2 items in DESIGN.md section 7 are not to be built yet, but must not be blocked.
- Anything marked "Verify against live" gets its findings written to `docs/verification.md`.

## Tests

`python -m pytest` from the repo root (install `requirements-dev.txt`). Add a test with every service, ingest or importer change. The poller takes an injected clock; drive it with `tests/fixtures/plex/*.json`. Schema changes need a migration in `bingestats/db.py` and a test that upgrades an old database.
