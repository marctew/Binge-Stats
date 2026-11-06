# Binge Stats — design

A self-hosted Plex monitoring and statistics app. A modern replacement for Tautulli: live activity, full play history, stats and graphs, notifications, plus things Tautulli doesn't do (binge and completion tracking, transcode analysis, library health, account-sharing detection, a per-user year in review, Home Assistant integration).

This document records decisions that are already made. Build to it. `SPEC.md` holds the build detail (schema, ingest rules, pages, API, milestones). Where something is marked **Verify against live**, check it against Marc's real Plex server or Tautulli database before committing to an approach, and write what you found in `docs/verification.md`.

## 1. Decisions

| Area | Decision |
|---|---|
| Media server | **Plex only.** No Jellyfin or Emby support. Keep Plex code in one package (`bingestats/plex/`) so it isn't smeared across the app, but don't build an abstraction for servers that won't exist. |
| Users | **One admin, plus viewer logins** (Marc changed this on 2026-10-31). The admin runs everything. The admin can give a person on the server a password. They then sign in with their Plex username and see **only their own** stats, history, binges, shows in progress and Wrapped (`/me`). Viewers can't see other people, settings, alerts, insights, the API or any stop button. There's no Plex OAuth sign-in. Wrapped share links stay public. |
| Database | **SQLite**, WAL mode, plain `sqlite3`, no ORM. Schema and migrations in `bingestats/db.py`. |
| History import | **Tautulli import is a v1 feature**, not an afterthought. Years of history must carry over. |
| Stack | Python 3.12, Flask, Jinja, HTMX, Alpine.js, Tailwind, ECharts. |
| Deployment | Docker Compose on an Ubuntu LXC. One image, two services (`web`, `worker`), one `/data` volume. |
| Sonarr / Radarr / SABnzbd | **Optional and strictly read-only** (Marc, 2026-10-31). The worker snapshots them every 3 h for real gaps in shows, film collections, below-cutoff lists, what's airing this week and download history. The clients have no write methods, so nothing in them can be deleted, unmonitored or changed. **One exception** (Marc, 2026-11-04): SABnzbd's `mode=switch`, to move jobs in the queue from Downloads, behind a Settings switch that's off by default, admin only. No delete, pause or priority call. Shows and films are matched to Plex by TVDB / TMDB id, not by title. |
| Pins (from Pinnarr) | **Pins live only in Binge, and Binge stays read-only** (Marc, 2026-11-05). Pinnarr's pins, calendar, episode states and season outlook move into Binge (`docs/specs/pins.md`). Its writes to other services are not ported: no Sonarr tag sync, no Plex Watchlist sync, no marking watched in Plex, no Overseerr requests. TMDB is added as an optional, GET-only source for the season outlook. |
| Outside API | **Built to Design Uniformity's API standard, as its pilot** (Marc, 2026-11-05). Displays, Docket, Home Assistant and TRMNL read `meta`, `status`, `feeds`, `images` and `events`, call a few named `actions`, and get signed webhooks and `/display/` pages; the shared code is lifted out of Binge into Design Uniformity afterwards. Binge's older `/api/v1` routes stay exactly as they are for the widget, the PC panel and the charts. Nothing destructive is offered as an action. Plan: `docs/specs/api-standard.md`; reference: `docs/API.md`. |
| Repo | `github.com/marctew/Binge-Stats` |

## 2. Environment

| Part | Detail |
|---|---|
| Host | Ubuntu LXC on Proxmox, Docker Compose |
| Plex | Existing Plex Media Server on the LAN (on Unraid). Reached by LAN URL + token |
| Access | LAN and Tailscale; may be published through a Cloudflare Tunnel or reverse proxy. Must work behind a proxy at `/` (no sub-path support needed) |
| Home Assistant | Mosquitto broker already in use. Integration is MQTT discovery |
| Timezone | Europe/London by default, configurable. Everything stored in UTC epoch seconds |

## 3. Architecture

```
            ┌──────────────── docker compose ────────────────┐
 Plex  ◄────┤ worker   poll /status/sessions every 5 s        │
 server     │          websocket nudge, library + user sync   │
            │          notifications, MQTT, jobs, backups     │
            │                │                                │
            │        /data/bingestats.db  (SQLite, WAL)       │
            │                │                                │
 Browser ◄──┤ web      Flask + gunicorn (gthread)             │
 HA (API)   │          UI, REST API, image proxy              │
            └─────────────────────────────────────────────────┘
```

- **The worker owns ingest.** It is the only process that writes sessions, history, library and user data. Exactly one worker runs. Running the poller inside gunicorn would duplicate it per worker process, which is why it's separate.
- **The web process reads, plus writes config.** Settings, rules, tokens, webhooks, jobs it creates, alert acknowledgements, pins, rotating a user's Wrapped share salt, and the API's idempotency keys and action log are its only writes. The API's events and webhook deliveries are written by the worker. Anything long-running (Tautulli import, library sync on demand, rebuilding derived tables) is queued as a row in `jobs` and the worker picks it up.
- **Two direct Plex calls from the web process are allowed:** terminating a stream (one request, the user is waiting on it) and the image proxy (`/img/` for the UI, and `/api/v1/images/` for API readers, which also resizes to WebP or JPEG). Nothing else from web talks to Plex. The image proxy also fetches Sonarr/Radarr posters (`/img/arr/...`, for downloads not yet in Plex), with their API keys kept server-side like the Plex token.
- **One other outside call from the web process:** the "Test" button on a notification channel sends a single message and shows the result straight away. All real notifications are sent by the worker.
- **Ingest is polling.** `GET /status/sessions` every 5 s (configurable 2–30 s). The Plex websocket (`/:/websockets/notifications`) is used only as a trigger to poll right away when playback state changes, so pause and stop are picked up in under a second. If the websocket drops, polling carries on alone. Plex webhooks are not used (they need Plex Pass and an inbound route).
- **Live activity in the browser is HTMX polling** of a partial every 3 s, with progress bars interpolated client-side by Alpine between refreshes. The UI uses no SSE and no WebSocket: one admin, a handful of tabs, and it survives Cloudflare and proxies without special handling.
- **Outside screens get server-sent events** (Marc, 5 Nov 2026, with the Design Uniformity API standard): `/api/v1/events` streams what the worker publishes to the `events` table, for Vital Signs, the panels and Home Assistant, with a 25 s ping so Cloudflare keeps it open. At most 4 streams per gunicorn worker (8 in all). The UI itself stays on HTMX polling.
- **The Plex token never reaches the browser.** Posters and art go through `/img/...` (and `/api/v1/images/...` for API readers) on the web process, which fetches from Plex, resizes and caches on disk.

## 4. What it does

v1 scope. Section numbers match the milestones in `SPEC.md`.

1. **Activity:** live sessions with poster, user, device, player, LAN/WAN, IP and location, progress, state, quality, a direct play / direct stream / transcode badge with the inferred reason, HW transcode flag, bandwidth. Totals bar on top. A stop button with a message for each stream.
2. **History:** every play, filterable by user, library, media type, decision, device, IP, date range and free-text title. Resumed plays are grouped into one row with a link to expand.
3. **Tautulli import:** reads a copy of `tautulli.db`, imports users, history, metadata and media info, keeps the resume grouping, is idempotent (safe to run twice), and gives a dry-run report first.
4. **Users:** list and detail pages: watch time, plays, favourite content, devices, IPs on a map, transcode share, viewing heatmap (hour × weekday).
5. **Libraries and items:** library stats, plus movie / show / season / episode pages with who watched what and when.
6. **Dashboard and graphs:** today and last-30-days tiles, plays and watch time over time, by day of week, by hour, by platform, by stream type, top users / movies / shows / platforms, concurrent streams peak.
7. **Notifications:** a rules engine (trigger → conditions → message template → channels) delivered through Apprise (Discord, Telegram, Pushover, ntfy, email and the rest). Includes a "recently added" digest that batches a season drop into one message.
8. **Home Assistant:** MQTT discovery entities for stream counts, bandwidth, server status, a per-user "now watching" sensor, and a per-user "stop streams" button.
9. **Stream rules and sharing detection:** per-user concurrent stream and transcode limits (notify or stop the newest stream with a message). Flags concurrent streams from different public IPs on one account, and a first play from a new country.
10. **Binges and completion:** binge runs (back-to-back episodes of one show), series completion % per user, started-and-abandoned shows.
11. **Transcode insights:** why things transcode, by user, device and file. A list of the files that cause the most transcoding, as re-encode candidates.
12. **Library health:** content nobody has watched in N months, never-watched content, storage per watched hour by show and movie. A "what to delete" view with sizes.
13. **Wrapped:** a year in review per user and for the whole server. A good-looking page the admin can share with a signed, revocable link.
14. **Pins and calendar:** each person pins the shows they follow and gets a calendar of just those, with honest episode states (awaiting, missing, in Plex), a season outlook (dated, announced, filming, hiatus, dormant, cancelled, ended), pin-scoped lists (ready to watch, retire, discover) and a message when a pinned episode is in Plex. See `docs/specs/pins.md`: the calendar and pinning are built (P1), the rest is to come.

## 5. UI

- **Dark first, with a light theme.** Card-based layout with plenty of space, artwork-led (posters and backdrops do the visual work), not table-dense. Tables only where the data is a table (history, library health).
- **Desktop and phone.** The sidebar collapses to a bottom bar on phones. No horizontal page scroll.
- **One component vocabulary:** stat tile, media card, session card, chart card, data table, filter bar, badge. Build these as Jinja macros and reuse them. No page-specific one-offs.
- **Charts are ECharts with one shared theme** (palette, fonts, tooltip style) in `static/js/charts.js`. Every chart has a loading state and an empty state.
- **Every list page has filters in the URL query string**, so views can be bookmarked and shared.
- **Static assets are pinned** by version and sha256 in `bingestats/static/vendor/VERSIONS`, and fetched at build time into the image. No CDN at runtime.
- **One exception: map tiles.** The location map loads basemap images from OpenStreetMap's tile servers (`tile.openstreetmap.org`) in the browser, darkened with a CSS filter in the dark theme. No API key is needed, and the traffic (one admin, user pages only) is well inside OSM's tile usage policy. Settings can point it at another tile URL, for example a provider with a key. CARTO was used until 2026-10-31, when it started requiring an API key. The map is the only page element that talks to a third party. It shows only on user pages, with attribution, and the page works without it.
- **Charts follow the dataviz rules.**
  - The categorical palette is validated for colour-blind separation against the app's own card colours, used in a fixed order, and gives each entity a fixed colour: stream type, media type, and users by all-time rank.
  - The heatmap uses a single-hue scale.
  - No dual axes, and no donut charts.

## 6. Security

- Single admin, password hashed with `werkzeug.security` (scrypt). First run lands on a setup wizard that creates the admin and connects Plex.
- Session cookie auth for the UI, CSRF tokens on every form and HTMX POST.
- REST API uses bearer tokens created in Settings, each stored hashed and given a name. Read-only or read-write. A **read** token may also go in the URL (`?token=`) for displays that can't set a header; a write token in the URL is refused. gunicorn's access log has paths without query strings and no referrer, so URL tokens never reach a log, and display pages send `Referrer-Policy: no-referrer`.
- 10 bad tokens in a minute from one address lock it out for 15 minutes (keyed by `CF-Connecting-IP` through the tunnel, since every tunnel request otherwise comes from cloudflared). 120 requests a minute per token. `API_PUBLIC=0` refuses the API through the tunnel altogether.
- Webhooks are signed (`X-Signature: sha256=` HMAC of the body), and their URLs are treated as secrets: only ever shown masked after saving.
- Optional "trusted proxy header" login (e.g. Cloudflare Access `Cf-Access-Authenticated-User-Email`) is off by default and only honoured from configured proxy IPs.
- The Plex token, the secret key and the MQTT password live in the database or in `/data`, never in the repo or in logs.
- Wrapped share links are signed with `itsdangerous`, and the signature includes a per-user salt, so the admin can revoke all of a user's links by rotating it.

## 7. Not in v1 (don't build, don't block)

- A newsletter (emailed "what's new" page).
- Overseerr (requester on history rows). Sonarr, Radarr and SABnzbd *are* built, read-only (Marc, 2026-10-31); see section 1.
- Multiple Plex servers in the UI. The schema carries `server_id` from day one, so adding this later is UI work only.
- Plex OAuth sign-in for users. Viewer logins exist (password set by the admin, own data only). Signing in with a Plex account, and viewers seeing anything beyond their own data, are still v2.
- Exports (CSV is in v1 for history. Anything more is v2).
- From Pinnarr: an MCP server, cast cross-referencing, TMDB "more like this" suggestions, episode ratings and an iCal feed of pins.
