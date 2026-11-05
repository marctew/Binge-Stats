# Spec: bringing the REST API to the API standard

Status: agreed 5 Nov 2026. Stage 1 built 5 Nov 2026; stages 2–6 not yet. Matches API standard draft 2.

## Why

Design Uniformity's `API-STANDARD.md` (draft 2, 5 Nov 2026, `marctew/design-uniformity` commit `8d3a7cc`) defines one outside API that every app offers, so that Docket briefs, Vital Signs, the ESP32 panels, Home Assistant, n8n and TRMNL can each have a reader written once.

**Binge is the pilot (decided, API standard sections 12 and 14).** Binge builds the standard first, against real data, and the generic parts are then lifted out into Design Uniformity's `du_api.py` with as little change as possible. Binge then moves onto the shared copy through the design updater. To keep that lift cheap, the generic code goes in one module with no Binge imports (see Layout below).

The standard isn't released to the apps yet, so `docs/design/API-STANDARD.md` isn't copied in. Build to draft 2 as it stands in the Design Uniformity repo. Draft 2 already takes in this plan's six changes (its section 16), so the two agree. Anything else the pilot finds goes back to Design Uniformity before the release (API standard section 14, step 2).

## Decisions

1. **Old endpoints stay exactly as they are.**
   - Every route in SPEC.md section 16 and `docs/API.md` keeps its path, shape and `{"error": {"code", "message"}}` errors.
   - The Scriptable widget (`docs/widget/binge-pins.js:67` reads `error.message`), the PC media panel and the dashboard charts use them.
   - The standard's endpoints are new routes beside them and use the standard's error shape `{"error": "…", "code": "…"}`.
   - Old routes do pick up the shared token handling (URL tokens, lockout, ETags, rate limit), since none of that changes their bodies.
2. **SSE is for outside readers only.**
   - DESIGN.md section 3 ("No SSE and no WebSocket") changes to: the UI stays HTMX polling; `/api/v1/events` is server-sent events for outside screens (API standard section 8).
   - Update that line when stage 4 lands.
3. **Events go through SQLite, not memory.**
   - Binge runs two gunicorn workers, and events start in the worker process (sessions, arrivals, alerts).
   - The worker writes each event to an `events` table. Each web worker's stream reads new rows from it.
   - `Last-Event-ID` is the row id.
4. **Viewer logins get nothing from the standard's endpoints.**
   - The standard lets any signed-in browser read `/api/v1/` GETs. In Binge that means the admin session only, as today. A viewer session is treated like no session.
   - The viewer allow-list rule in CLAUDE.md is unchanged.
5. **No stop-stream action.**
   - Stopping a stream is destructive (standard section 7), so it isn't offered as an action.
   - The old `POST /activity/{id}/stop` stays for the PC panel.
   - Add `stop_session` only if Marc asks for it.
6. **Time.**
   - Binge stores and serves epoch seconds. The standard's shapes use ISO 8601 UTC (`…Z`) plus `at_label`, which is written in `ui.timezone`.
   - The conversion happens only at the edge, in the new endpoints. Nothing in the database changes.

## Layout

- **`bingestats/web/api_core.py`**: the generic part, later lifted into `du_api.py`. It imports nothing from Binge; it takes callables. It holds:
  - the token check (header, `?token=`, write tokens refused in the URL, scope check);
  - the bad-token lockout and the per-token rate limit;
  - `API_PUBLIC` handling;
  - error helpers and the standard's error codes;
  - ETag/304 and `Cache-Control`;
  - the registry behind `meta` (`feed`, `action`, `display` decorators and an event-type list);
  - the idempotency wrapper;
  - the SSE generator (given a "rows after id" function);
  - the image resizer (Pillow) and its disk cache.

  It talks to Binge only through an `init_api(app, data_dir, *, name, version, sha, accent, icon, time_zone, check_token, ...)` call.
- **`bingestats/web/routes_api_std.py`**: the standard's routes (`meta`, `status`, `feeds`, `images`, `actions`, `events`), registered on the existing `api` blueprint so the `/api/v1` prefix and the auth gate's `api.` exemption still apply. They stay thin and call services.
- **`bingestats/web/routes_display.py`**: the `/display/` blueprint. Its endpoints are exempted in `auth.install()` the same way `api.*` is, and it checks tokens through `api_core`.
- **`bingestats/services/api_feeds.py`**: builds `status` and each feed in the common shapes from the existing services (`activity`, `history`, `alerts`, `arr`, `pin_api`, `pin_lists`).
- **`bingestats/services/events.py`**: `publish(conn, type, title, item)`, `after(conn, last_id, limit)`, `prune(conn, now)`, and the mapping from notification triggers to event types.
- **`bingestats/services/webhooks.py`**: CRUD for the Settings card, signing, and the delivery queue. Deliveries are sent by the worker, never in a request.
- **`bingestats/services/api_audit.py`**: the action log.

## Migration `_m14`

```sql
CREATE TABLE events (
  id INTEGER PRIMARY KEY,           -- Last-Event-ID
  type TEXT NOT NULL,               -- dotted, e.g. playback.start
  at INTEGER NOT NULL,              -- epoch s
  title TEXT NOT NULL,
  item TEXT NOT NULL                -- JSON, item shape
);
CREATE INDEX events_at ON events (at);

CREATE TABLE webhooks (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
  secret TEXT NOT NULL, event_types TEXT NOT NULL,   -- JSON list; [] = all
  enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
  last_ok_at INTEGER, last_error TEXT, last_error_at INTEGER
);
CREATE TABLE webhook_deliveries (
  id INTEGER PRIMARY KEY, webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
  event_id INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  next_at INTEGER NOT NULL, done_at INTEGER, error TEXT
);
CREATE INDEX webhook_deliveries_due ON webhook_deliveries (done_at, next_at);

CREATE TABLE api_idempotency (
  token_id INTEGER NOT NULL, key TEXT NOT NULL, action TEXT NOT NULL,
  status INTEGER NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY (token_id, key)
);
CREATE TABLE api_audit (
  id INTEGER PRIMARY KEY, at INTEGER NOT NULL, token_id INTEGER, token_name TEXT NOT NULL,
  action TEXT NOT NULL, body TEXT NOT NULL, status INTEGER NOT NULL, message TEXT
);
```

- The webhook URL and secret are stored like the Plex token. After saving, Settings shows only the masked URL (`notify.mask_url`).
- Write ownership, so the CLAUDE.md process rule holds:
  - **web process:** `api_idempotency`, `api_audit`, `webhooks` (Settings), and `api_tokens.last_used_at` (as today);
  - **worker:** `events`, `webhook_deliveries`, and pruning.
- Add this list to CLAUDE.md's worker line.
- Pruning, done by the worker:
  - `events` older than 1 hour, keeping at least the last 200;
  - `api_idempotency` older than 24 hours;
  - `api_audit` older than 90 days;
  - finished deliveries older than 7 days.
- Test: an old (m13) database upgrades and keeps its tokens.

## Stage 1: core, `meta`, `status`

**Token handling (`api_core`), for the whole `/api/v1/` and `/display/`:**
- `Authorization: Bearer` as now. Also `?token=`, for read tokens only. A write token in the URL gets `400 {"error": "Write tokens go in the Authorization header, never the URL.", "code": "bad_request"}`, even on a GET.
- Bad token: `401 {"error": "That token isn't valid. Make a new one in Settings → API tokens.", "code": "unauthorised"}`. A right token with the wrong scope gets `403`, code `forbidden`.
- **Lockout:** 10 bad tokens from one client IP (after ProxyFix) in 60 s gets `429` with `Retry-After` for 15 minutes. While locked out, even good tokens from that IP are refused.
- **Rate limit:** 120 requests a minute per token, then `429` with `Retry-After`. Images and events don't count.
- Both limits are in memory, per gunicorn worker. Two workers make them about 2× looser, which is fine against 256-bit tokens. Note it in `docs/API.md`.
- **`API_PUBLIC`:** env var, default `1`. When it's `0`, any `/api/v1/` or `/display/` request carrying `CF-Connecting-IP` gets `403`, code `forbidden`. Add it to the compose file and README.
- **Logging:** tokens must never reach a log. In `docker/entrypoint.sh`:
  - set gunicorn's `--access-logformat` to `'%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"'` (the path without the query string, and no referrer);
  - check Binge's own logging never prints `request.url` or `request.full_path` for these paths;
  - `/display/` pages send `Referrer-Policy: no-referrer`.
- **ETags:** every GET under `/api/v1/` (old and new) gets a weak ETag of the body, and `If-None-Match` gets `304`. `status` and feeds also send `Cache-Control: private, max-age=<refresh_s // 2>`.
- **Errors on the new routes:** `{"error": "<sentence>", "code": "<code>"}` using the standard's codes: `bad_request`, `unauthorised`, `forbidden`, `not_found`, `rate_limited`, `busy`, `upstream`, `error`. `429` and `503` always carry `Retry-After`. Plex, Sonarr, Radarr, SABnzbd and TMDB failures are `502 upstream`.

**`GET /api/v1/meta`** (read):
- `app: "binge-stats"`, `name`, `version` (`__version__`), `sha` (`BS_SHA`), `api: 1`, `accent: "violet"`, `icon`, and `time_zone` (`ui.timezone`).
- `feeds`, `actions`, `displays` and `events`, all from the registry.
- `move_download` is listed only while `sabnzbd.allow_moves` is on.

**`GET /api/v1/status`** (read), in the standard's status shape, `refresh_s: 15`:

| `state` | When |
|---|---|
| `error` | Plex isn't configured or is down, or the worker isn't alive (`activity.worker_status`) |
| `warning` | there are unacknowledged alerts, or Sonarr, Radarr, SABnzbd or TMDB failed its last read (the `failing` list from `pin_api.summary`) |
| `busy` | an import or another long job is running (the same query as `/healthz`) |
| `ok` | otherwise |

- `headline`: one sentence that stands on its own. In priority order:
  - "Plex is down" / "The worker isn't running";
  - "3 streams, 1 transcoding" when something is playing;
  - otherwise "Nothing playing".
- `detail`: optional. The first stream ("Marc · Slow Horses S5 E3, 64%"), or the warning's reason.
- `counts`, keys fixed from now on:
  - `streams`
  - `transcodes`
  - `bandwidth_mbps`
  - `alerts` (unacknowledged, `tone: "warning"` when above 0)
  - `downloads`
  - `pins_ready` (episodes waiting, the Plex owner's)
- `url`: `ui.public_url`, else the request host.
- Must answer from what's in the database. It never calls Plex or the *arrs in the request.

**Tests:**
- every token path: header, URL read, URL write refused, bad, wrong scope, viewer session refused, admin session allowed;
- lockout and its expiry (injected clock);
- the rate limit;
- `API_PUBLIC=0` with and without `CF-Connecting-IP`;
- ETag and 304;
- `meta` lists only what's registered;
- each `status` state from fixture data;
- the gunicorn log format has no `%(r)s`, `%(q)s` or `%(f)s` (a plain text check of `entrypoint.sh`).

## Stage 2: feeds and images

**Feeds:**
- `GET /api/v1/feeds/<name>` (read) uses the standard's feed and item shape.
- They all take `limit` (default 20, max 100), `since` (ISO time) and `cursor`. `since` and `cursor` work only where the feed is ordered by time.
- An unknown feed gets `404 not_found` with the list of feed names in the sentence.

| Feed | Items | `refresh_s` | Own params | `meta` fields |
|---|---|---|---|---|
| `now_playing` | active sessions: title, subtitle "User · Device", `at` = started, `tone` `warning` if transcoding | 10 | — | `user_id`, `user_name`, `progress_pct`, `state` (playing/paused/buffering), `decision`, `bandwidth_kbps`, `player`, `rating_key` |
| `arrivals` | recently added, newest first: films, episodes (grouped as in the digest), albums | 300 | `kind=movie\|show\|album` | `rating_key`, `library_id`, `kind`, `show_title`, `season`, `episode` |
| `history` | finished plays, newest first | 60 | `user_id`, `kind` | `user_id`, `user_name`, `rating_key`, `play_s`, `decision`, `player` |
| `alerts` | unacknowledged alerts (all with `all=1`) | 60 | `all` | `alert_id`, `kind`, `user_id`, `acknowledged` |
| `downloads` | SABnzbd TV and film jobs in queue order | 30 | — | `nzo_id`, `pct`, `eta_s`, `stalled`, `size_mb`, `category`, `app` (sonarr/radarr) |
| `pins_schedule` | pinned episodes `back` days ago to `days` ahead, ordered by air time | 300 | `user_id`, `days`, `back` | `tvdb_id`, `code`, `state`, `downloading_pct`, `stalled` |
| `pins_ready` | per show, the next episode to watch | 300 | `user_id` | `tvdb_id`, `next`, `waiting`, `new` |

- Item ids: `session:<id>`, `plex:<rating_key>`, `play:<id>`, `alert:<id>`, `sab:<nzo_id>`, `episode:<tvdb_episode_id or show-code>`, `show:<tvdb_id>`.
- `image` points at the image route below. `url` is the Binge page for the thing, on `ui.public_url`.
- `at_label`: written in `ui.timezone` by a new `filters.at_label(ts, now)`:
  - "Now" (under a minute);
  - "Today 19:12";
  - "Yesterday 19:12";
  - "Sat 7 Nov" within the coming or past week;
  - "7 Nov 2025" otherwise;
  - future times as "in 2 h" when under 12 hours.

  Tests cover each band and a DST change.
- Plex, Sonarr and Radarr secrets never appear: no Plex paths with tokens, no *arr URLs that need keys.

**Images:**
- `GET /api/v1/images/<kind>/<id>` (read, same token as the feed, URL token allowed). Kinds:
  - `poster/<rating_key>` and `art/<rating_key>` (Plex, through `routes_img.plex_image_response`'s fetch);
  - `show/<tvdb_id>` (a pinned or Sonarr show's poster: the logic of `/pins/poster`).
- `w=` or `h=` snaps to 80, 160, 320, 640 or 1280.
- The answer is `image/webp`, or `image/jpeg` when the reader's `Accept` doesn't list `image/webp`. `fmt=jpeg|webp`, `q=40..95` (default 80) and `gray=1` are honoured.
- Resizing is done with Pillow in `api_core`. Plex hands over its image at the nearest width ≥ the target, and Pillow converts it.
- Cache files go in `/data/image-cache/`, named by the sha256 of kind, id, w, fmt, q and gray (API standard section 6). Send an ETag and `Cache-Control: public, max-age=86400`. Pruning is LRU by mtime over 500 MB, done by the worker.
- Add Pillow to `requirements.txt`, pinned. It's a Python dependency, not a front-end asset, so it doesn't go in `vendor/VERSIONS`.
- The old `/img/` and `/pins/poster/` routes are unchanged.

**Tests:**
- every feed's shape against `tests/fixtures`;
- `limit`, `since` and `cursor`;
- the unknown-feed 404;
- no `X-Plex-Token` substring anywhere in any feed (a test that walks every registered feed);
- images: WebP vs JPEG by `Accept`, the width snap, `gray=1` output being single-channel, a cache hit giving the same ETag, a URL write token refused.

## Stage 3: actions

**Actions:**
- `POST /api/v1/actions/<name>`: write token in the header only. A signed-in admin session also works, with `X-CSRF-Token`, so the API tokens card can show a test button later.
- Answer: `{"ok": true, "message": "…", "result": {...}}`.

| Action | Body | Does | `message` |
|---|---|---|---|
| `pin_show` | `{tvdb_id, user_id?}` | `pins.toggle` if not pinned | "Pinned Slow Horses." / "Slow Horses was already pinned." |
| `unpin_show` | `{tvdb_id, user_id?}` | `pins.unpin` | "Unpinned Slow Horses." |
| `ack_alert` | `{alert_id}` | `alerts.acknowledge` | "Alert cleared." |
| `ack_all_alerts` | `{}` | `alerts.acknowledge` | "Cleared 3 alerts." |
| `move_download` | `{nzo_id, to? , above?, below?}` | `arr.move`; listed and allowed only with `sabnzbd.allow_moves` | "Moved Slow Horses S5E3 to the top." |

- `user_id` defaults to the Plex owner, as in `pin_api.resolve_user`.

**Idempotency:**
- An `Idempotency-Key` header (1–100 chars) is stored with the answer for 24 hours, per token.
- A repeat with the same key gives back the stored status and body and does nothing.
- The same key with a different action gets `400 bad_request`.

**Audit:**
- Every action call, including refused ones, writes an `api_audit` row with the token's name (or "Admin session").
- The API tokens card in Settings gets a "Recent API actions" list (the last 20) under the tokens table, built with the existing list macro and with an empty state.

**Tests:**
- each action's happy path and its 4xx errors;
- `move_download` refused and missing from `meta` while `allow_moves` is off;
- the idempotency replay does nothing twice (count the rows);
- a write token in the URL is refused;
- a read token gets 403;
- audit rows for success and for refusal.

## Stage 4: events and webhooks

**Publishing:**
- `Worker.on_event` (`worker/main.py`) already sees every notification trigger. After `notifier.handle`, it calls `events.publish` with the mapped type and the item built by the same code as the matching feed.
- Trigger to event type:

| Trigger | Event |
|---|---|
| `play_start`, `play_stop`, `pause`, `resume`, `buffer`, `watched` | `playback.start`, `.stop`, `.pause`, `.resume`, `.buffering`, `.watched` |
| `transcode_start` | `playback.transcode` |
| `recently_added` | `arrival` (one event per item in the digest) |
| `server_down`, `server_up` | `server.down`, `server.up` |
| `stream_limit`, `transcode_limit`, `concurrent_ips`, `new_country` | `alert.raised` (`meta.kind` says which) |
| `pin_arrived` | `pin.arrived` |
| other pin triggers (air date moved, week, discover) | `pin.<trigger>` |

- Also publish `download.finished` from `DownloadWatcher`, when a TV or film job leaves the queue as completed.
- The list comes from one dict in `services/events.py`. `meta.events` reads it, and a test checks that every key in `notify.TRIGGERS` is mapped.

**Stream: `GET /api/v1/events`** (read, URL token allowed), `text/event-stream`:
- `event: <type>`, `id: <row id>`, `data: {"type", "at" (ISO Z), "title", "item"}`.
- On connect, it replays rows after `Last-Event-ID` (header, or `?last_event_id=`), up to 200. Without one, it replays nothing.
- Then it checks for new rows every 1 s, with a read-only query on its own connection.
- `: ping` every 25 s.
- **Capacity:** at most 4 open streams per gunicorn worker (8 in total). Beyond that, `503` with `Retry-After: 30`.
- Raise gunicorn to `--threads 12` so 4 streams leave 8 threads for everything else.
- On `SIGTERM` the generator ends, so `scripts/update.sh` restarts don't hang.

**Webhooks:**
- A **Webhooks** card in Settings, after API tokens and before the Design system card.
  - Each webhook has a name, a URL (validated with `notify.validate_url`'s rules but HTTP(S) only), event types (checkboxes from the event list; none ticked means all) and a secret (generated, shown once, can be regenerated).
  - Each one shows its last success and last error.
- Delivery happens in the worker. A new event makes one `webhook_deliveries` row per matching webhook.
  - Each delivery is a POST of the stream's `data` JSON with `Content-Type: application/json` and `X-Signature: sha256=<hex HMAC-SHA256 of the raw body with the secret>`, with a 10 s timeout.
  - A failed attempt is retried at +1, +3 and +6 minutes (3 retries over about 10 minutes), then given up with `last_error` set.
  - The worker checks for due deliveries once a second.
- A Home Assistant webhook URL (`/api/webhook/<id>`) is a secret. Store it like the Plex token and show it masked.

**Docs:** DESIGN.md section 3 changes as in decision 2. SPEC.md section 9 gets a line that notifications (Apprise, for people) and webhooks (signed, for machines) are separate.

**Tests:**
- every trigger is mapped;
- `publish` then `after` round trip;
- the SSE generator (driven with an injected clock and a fake sleep): replay from `Last-Event-ID`, ping timing, the cap giving 503;
- the webhook signature against a known vector;
- the retry schedule;
- event-type filtering;
- the masked URL in Settings HTML.

## Stage 5: display page

**`GET /display/now-playing`** (read token, URL allowed; an admin session also works for previewing):
- What's playing now: poster, title, user, progress bar, and a transcode badge. When idle: "Nothing playing" plus the last 3 arrivals.
- Full viewport, no sidebar, no scroll bars. Uses Design Uniformity tokens through the Tailwind preset, with the violet accent.
- Params, as in the standard's section 9:
  - `theme=dark|light` (default dark)
  - `size=` (a layout hint: three breakpoints for 320x240, 800x480 and 1920x1080)
  - `scale=`
  - `accent=` (any DU accent name; anything else is ignored)
  - `static=1` (rendered on the server, no JS)
- Without `static=1`:
  - a small inline script listens to `/api/v1/events?token=…` for `playback.*` and re-fetches `/api/v1/feeds/now_playing`;
  - it falls back to polling at `refresh_s`;
  - if Binge stops answering, it keeps what it shows and adds a faint "Last updated 14:02".
  - No Alpine or HTMX on this page, to keep it light for Pis.
- Images use the API image route with the same token.
- The shell is `templates/display/base.html`: tokens, theme, scale, the "Last updated" note and the events client. It's written to be lifted into Design Uniformity unchanged, so it has no Binge-specific markup.
- A "Display pages" line on the API tokens card links to `/display/now-playing` as a preview.

**Tests:**
- 401 without a token;
- renders with a URL token;
- `static=1` has no `<script>`;
- `Referrer-Policy: no-referrer`;
- the token never appears in the page except in the events and image URLs it needs;
- an unknown `accent` is ignored.

A second display (pins "Up next", built from `pins_schedule`) waits until Marc picks it (standard open question 2).

## Stage 6: docs and checking

- **`docs/API.md` covers the whole API:**
  - the standard's part first: auth, URL tokens, limits, `API_PUBLIC`, every feed with its `meta` fields, every action with its body, every event type, and the display page with its params;
  - then the existing endpoints moved over from SPEC.md section 16, still with `{"error": {"code", "message"}}`, marked "Binge's own; kept for the widget and panel".
  - SPEC.md section 16 becomes a pointer to it.
- **`scripts/check-api.sh <base_url> <read_token>`** does what the standard's `check_api.py` will:
  - `meta` has the required keys;
  - the `status` shape;
  - one feed's item shape;
  - ETag then 304;
  - a bad token gets 401;
  - a write token in the URL gets 400.

  It's a placeholder until Design Uniformity ships `check_api.py`; then delete it.
- **CLAUDE.md:** add the new tables to the web and worker write lines. Add "The outside API follows `docs/design/API-STANDARD.md` once it's copied in; until then `docs/specs/api-standard.md`."
- **Verify against live** (write it up in `docs/verification.md`):
  - `status` and `now_playing` against a real stream;
  - an arrival event reaching a Home Assistant webhook trigger;
  - the stream surviving 5 minutes through the Cloudflare tunnel;
  - the display page on a Vital Signs Pi;
  - WebP and JPEG on the ESP32 panel decoder.

## Order and size

Stages 1–3 are one PR each and don't depend on stage 4. Stage 4 is the biggest: migration, worker, web and Settings. Stage 5 needs stages 2 and 4. Stage 6 goes with stage 5. Keep `_m14` in stage 1 with all its tables, so there's one migration and not three.

## Checked against

`API-STANDARD.md` draft 2 at `8d3a7cc`, and `APP-STANDARD.md` v1.7 plus that commit's access-log section.
