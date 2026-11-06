# REST API

Binge has two kinds of route under `/api/v1/`:

- **The API standard's routes** (Design Uniformity `API-STANDARD.md`, draft 2; plan in `docs/specs/api-standard.md`): the same shape in every one of Marc's apps, for displays, Docket, Home Assistant and TRMNL. Errors are `{"error": "<sentence>", "code": "<code>"}`.
- **Binge's own routes** (the second half of this file), kept exactly as they were for the widget, the PC panel and the dashboard's charts. Errors are `{"error": {"code", "message"}}`.

This file is the whole API. Check a running Binge against the standard with `scripts/check-api.sh`.

## Tokens, for every route

- `Authorization: Bearer <token>`, from Settings → API tokens. Read tokens read; write tokens read and write.
- **Read tokens may go in the URL** as `?token=…`, for displays that can't set a header. A write token in the URL is refused with `400`, even on a GET. URL tokens never reach the access log: gunicorn logs the path without the query string, and no referrer.
- A signed-in admin can read without a token (that's how the dashboard's charts load). Viewer logins can't use the API at all.
- **Limits:** 10 bad tokens from one address in a minute locks that address out for 15 minutes (`429` with `Retry-After`). Through the Cloudflare tunnel the address is `CF-Connecting-IP`, so one stranger can't lock out every tunnel reader. Each token gets 120 requests a minute, then `429`. Both are counted per gunicorn worker, so with two workers they're about twice as loose; that's accepted (API standard section 10).
- **Through the tunnel:** `API_PUBLIC: "0"` in the compose file refuses any `/api/v1/` request that came through Cloudflare (`CF-Connecting-IP` set) with `403`. The default, `1`, answers there too, always with a token.
- **Caching:** every successful GET has a weak `ETag`; send it back as `If-None-Match` and an unchanged answer is `304` with no body.
- `/api/v1/health` needs no token.

## The standard's routes

| Method | Path | Answers |
|---|---|---|
| GET | `/meta` | Who Binge is and what it offers: `app`, `name`, `version`, `sha`, `api` (1), `accent`, `icon`, `time_zone`, and the lists `feeds`, `actions`, `displays`, `events` |
| GET | `/status` | The one-glance summary, below. `Cache-Control: private, max-age=7` |
| GET | `/feeds/{name}` | A list in the common item shape, below |
| GET | `/images/{kind}/{id}` | Artwork, resized here: `poster/{rating_key}`, `art/{rating_key}`, `show/{tvdb_id}` |
| POST | `/actions/{name}` | One named thing to do, below. Write token, in the header only |
| GET | `/events` | Server-sent events: live changes, below |

And one display page, outside `/api/v1`: `GET /display/now-playing`, below.

**`/status`**, from the database only (it never waits on Plex or the *arrs):

```json
{"app": "binge-stats", "state": "ok", "headline": "2 streams, 1 transcoding",
 "detail": "Marc · Slow Horses S05E03, 64%",
 "counts": [{"key": "streams", "label": "Streams", "value": 2},
            {"key": "transcodes", "label": "Transcoding", "value": 1},
            {"key": "bandwidth_mbps", "label": "Bandwidth (Mbps)", "value": 18.4},
            {"key": "alerts", "label": "Alerts", "value": 0},
            {"key": "downloads", "label": "Downloading", "value": 3},
            {"key": "pins_ready", "label": "Ready to watch", "value": 312}],
 "url": "https://binge.example.com/", "generated_at": "2026-11-05T22:40:00Z", "refresh_s": 15}
```

- `state`: `error` (Plex not set up, Plex down, or the worker not running; the headline says which), else `warning` (unacknowledged alerts, or Sonarr, Radarr, SABnzbd or TMDB failed its last read; `detail` says what), else `busy` (an import or other long job is running), else `ok`.
- `headline` is what's playing ("Nothing playing" when nothing is) unless `state` is `error`.
- `counts` keys are fixed: a Home Assistant REST sensor can read `counts[key=...]`. `alerts` has `"tone": "warning"` when above 0. `downloads` is TV and film jobs in SABnzbd's queue; `pins_ready` is episodes waiting for the Plex owner.

### Feeds

Every feed answers `{"feed", "title", "items", "next_cursor", "generated_at", "refresh_s"}` and takes `limit` (default 20, max 100). Feeds ordered by time also take `since` (ISO time: items after it) and `cursor` (the last page's `next_cursor`). `Cache-Control` is half of `refresh_s`.

Every item has `id` and `title`; any of `subtitle`, `at` (UTC, `...Z`), `at_label` ("Today 19:12", "Sat 7 Nov", "in 2 h", written in Binge's timezone), `tone` (`accent`, `success`, `warning`, `danger`), `image`, `url` and `meta` may be there. Fields with no value are left out.

| Feed | Items | `refresh_s` | Params | `meta` |
|---|---|---|---|---|
| `now_playing` | Each stream: title, subtitle "S05E03 · Ep · User · Player", `at` = started, `tone: warning` when transcoding | 10 | — | `user_id`, `user_name`, `progress_pct`, `state`, `decision`, `bandwidth_kbps`, `player`, `rating_key`, `media_type` |
| `arrivals` | Newest first: films, albums, and episodes of one season added within 6 hours of each other as one item ("Season 5 · 3 new episodes") | 300 | `kind=movie\|show\|album`, `since`, `cursor` | `kind`, `rating_key`, `library_id`, and for shows `show_id`, `show_title`, `season`, `episode`, `episodes` |
| `history` | Finished plays, newest first; `at` = stopped | 60 | `user_id`, `kind=movie\|episode\|track`, `since`, `cursor` | `user_id`, `user_name`, `rating_key`, `media_type`, `play_s`, `decision`, `player`, `watched` |
| `alerts` | Unacknowledged alerts, newest first | 60 | `all=1` for acknowledged ones too, `since`, `cursor` | `alert_id`, `kind`, `user_id`, `acknowledged` |
| `downloads` | SABnzbd's TV and film jobs, in queue order; `at` = SABnzbd's finish estimate; `tone: warning` when stalled | 30 | — | `nzo_id`, `pct`, `eta_s`, `stalled`, `size_mb`, `category`, `app`, `pinned`, `rating_key` |
| `pins_schedule` | Pinned episodes in air order; `at` = air time | 300 | `user_id`, `days` (0–60, 7), `back` (0–30, 1) | `tvdb_id`, `code`, `season`, `episode`, `state`, `downloading_pct`, `stalled`, `rating_key` |
| `pins_ready` | Per pinned show, the next episode to watch, newest arrival first | 300 | `user_id` | `tvdb_id`, `next`, `next_rating_key`, `waiting`, `new` |

`user_id` on the pins feeds defaults to the Plex server owner.

### Images

`GET /api/v1/images/poster/{rating_key}?w=320`. An episode or season gets its show's poster, a track its album's. `art/{rating_key}` is the background art; `show/{tvdb_id}` is a pinned show's poster from Plex, else Sonarr.

- `w=` (or `h=`) snaps to 80, 160, 320, 640 or 1280. Images are never made bigger than the original.
- WebP if the request's `Accept` lists `image/webp`, else JPEG (for ESP32 decoders). `fmt=webp|jpeg` forces one, `q=40..95` sets the quality (80), `gray=1` gives greyscale for e-ink.
- `Cache-Control: public, max-age=86400` and an `ETag`. They're cached in `/data/image-cache/`; the worker trims it, oldest first, above 500 MB.
- An image path in a feed works with the same token the feed was read with, URL token included. Images don't count towards the rate limit.

### Actions

`POST /api/v1/actions/{name}` with a JSON object as the body (an empty body counts as `{}`). A write token, sent as `Authorization: Bearer`; a write token in the URL is refused. A signed-in admin can also call them, with the `X-CSRF-Token` header.

The answer is `{"ok": true, "message": "Pinned Slow Horses.", "result": {...}}`. `message` is short enough for a toast or a panel's status line. Refusals use the standard's error shape with `bad_request` (400), `forbidden` (403), `not_found` (404) or `upstream` (502, SABnzbd or Plex said no).

| Action | Body | `result` |
|---|---|---|
| `pin_show` | `{"tvdb_id": 412, "user_id": 5342218}`; `user_id` defaults to the Plex owner | `tvdb_id`, `user_id`, `pinned`, `changed` (false if it was already pinned) |
| `unpin_show` | the same | the same, `pinned: false` |
| `ack_alert` | `{"alert_id": 17}` | `alert_id` |
| `ack_all_alerts` | `{}` | `acknowledged`: how many |
| `move_download` | `{"nzo_id": "SABnzbd_nzo_x", "to": "top"}` (`top` or `bottom`), or `above` / `below` with the neighbouring job's id | what SABnzbd reported. Only listed in `meta`, and only allowed, while Settings → SABnzbd → "Allow moving jobs" is on; otherwise `403` |

Nothing destructive is offered: there's no action to stop a stream (the older `POST /api/v1/activity/{id}/stop` is still there for the PC panel).

- **`Idempotency-Key`** (any string up to 100 characters): send the same key again within 24 hours, from the same token, and you get the first answer back with nothing done twice. Refusals are remembered too; an upstream failure (502) isn't, so it can be retried with the same key. The same key for a different action is a `400`.
- **The action log:** every call, refused ones included, is logged with the token's name and shown under **Recent API actions** in Settings → API tokens. Kept 90 days.

### Events

`GET /api/v1/events` is `text/event-stream`, with a read token (header or `?token=`). Each event is

```
event: playback.start
id: 4182
data: {"id": 4182, "type": "playback.start", "at": "2026-11-05T22:06:00Z", "title": "Marc started Slow Horses", "item": {...}}
```

where `item` is in the feeds' item shape.

- Send `Last-Event-ID` (the header, or `?last_event_id=`) when reconnecting to get up to 200 events you missed from the last hour. Without one, nothing is replayed; you get what happens from now.
- `: ping` every 25 seconds, so Cloudflare and other proxies keep it open. The stream also ends after an hour (and at once when Binge restarts); reconnect with `Last-Event-ID` and nothing is lost. `retry: 5000` tells browsers to wait 5 s.
- At most 8 streams are open at once (4 per gunicorn worker); beyond that, `503` with `Retry-After: 30`. Streams don't count towards the rate limit.

| Type | When |
|---|---|
| `playback.start`, `.stop`, `.pause`, `.resume`, `.buffering`, `.watched`, `.transcode` | A stream changed. `item` as in `now_playing`; `.stop` adds `play_id`, `play_s`, `watched` to `meta` |
| `arrival` | Something landed in Plex (after the same quiet period as the Recently added notification); a season drop is one event |
| `server.down`, `server.up` | Plex stopped or started answering |
| `alert.raised` | A stream or transcode limit, two places at once, or a new country; `meta.kind` says which |
| `pin.arrived`, `pin.schedule_changed`, `pin.week`, `pin.discover` | The pin notifications, for the person in `meta.user_id` |
| `download.finished` | A TV or film job Sonarr or Radarr knows about completed in SABnzbd |

### Webhooks

Settings → **Webhooks**: a name, a URL and, optionally, which event types to send (none ticked: all). Binge POSTs each event as JSON, the same object as the stream's `data`, with:

- `X-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed with the webhook's secret>`. The secret is shown once when the webhook is made (or given a new one).
- `X-Event-Type` and `X-Event-Id`.
- A 10-second timeout; anything but a 2xx is a failure. A failure is retried after 1, 3 and 6 minutes, then dropped; Settings shows each webhook's last success and last error.

Sending happens in the worker, never in a request. The URL is treated as a secret (a Home Assistant webhook id is the whole credential), so Settings only ever shows it masked. Home Assistant's own webhook trigger works as is: `http://homeassistant.lan:8123/api/webhook/<id>`. Binge is on the LAN, so the trigger's default `local_only: true` is fine.

Checking the signature in Python:

```python
import hashlib, hmac
ok = hmac.compare_digest(request.headers["X-Signature"],
                         "sha256=" + hmac.new(SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest())
```

### Display page

`/display/now-playing?token=<read token>` is a full-screen page in Binge's look for a kiosk, a Vital Signs screen or the widget display. It has no sidebar, no login and no scroll bars, and it fills whatever size it's given. It shows what's playing: one stream large, several as cards. When nothing is playing it shows the last three arrivals.

| Param | Does |
|---|---|
| `token` | A read token (each screen its own, so it can be revoked alone). A signed-in admin can open it without one to preview it |
| `theme=dark\|light` | Default dark |
| `size=800x480` | A layout hint: up to 480 wide is small (one column, fewer details), up to 1100 is medium, wider is large. The page still fills the window |
| `scale=1.5` | Everything bigger, for a screen across the room (0.5 to 3) |
| `accent=blue\|graphite\|teal\|fuchsia` | Use another Design Uniformity accent family, so a wall of screens can match. Anything else is ignored |
| `static=1` | Rendered once, with no JavaScript: for e-ink and screenshot services |

Without `static=1`, the page reloads its own body every 10 seconds, and straight away when a `playback.*` or `arrival` event comes in on `/api/v1/events`. If Binge stops answering, it keeps what it's showing and adds a faint "Last updated 14:02". Its images come from `/api/v1/images/` with the same token, sized for the screen.

The page sends `Referrer-Policy: no-referrer` and `Cache-Control: no-store`, since its token is in its URLs. Errors (no token, a bad one) come back as a plain page, not JSON. It's listed in `/api/v1/meta` under `displays`.

Home Assistant:

```yaml
rest:
  - resource: http://binge.lan:8181/api/v1/status
    headers: {Authorization: "Bearer bs_..."}
    scan_interval: 15
    sensor:
      - name: Binge Stats
        value_template: "{{ value_json.state }}"
        json_attributes: [headline, detail, counts]
```

# Binge's own routes

Binge's API from before the standard, kept exactly as it was: the dashboard's charts, the PC media panel and the Scriptable widget use it. Same tokens and limits as above. Errors are `{"error": {"code", "message"}}`. Times are UTC epoch seconds. Lists take `limit` and, where they page, `cursor` (the last answer's `next_cursor`).

Ranges: the `stats`, `insights` and `users/{id}/stats` routes take `range` (`7d`, `30d` (the default), `90d`, `1y` or `all`) or `from` and `to` (`YYYY-MM-DD`, in Binge's timezone), plus `user_id` and `library_id` where it makes sense. `metric` is `plays` or `watch_time`.

| Method | Path | Answers | Scope |
|---|---|---|---|
| GET | `/health` | `{ok, worker_alive, plex_up, plex_configured, version}`. No token | none |
| GET | `/activity` | `{totals, sessions}`: what's playing, with progress | read |
| POST | `/activity/{session_id}/stop` `{message}` | Stops that stream in Plex | write |
| POST | `/downloads/{nzo_id}/move` `{to}` or `{above, below}` | Moves a SABnzbd job; `403` unless moves are on in Settings | write |
| GET | `/history?user_id&type&decision&from&to&q&limit&cursor` | `{items, next_cursor}` | read |
| GET | `/users`, `/users/{id}` | Users (no share salts) | read |
| GET | `/users/{id}/stats?range` | `{summary, heatmap, top_shows, top_movies}` | read |
| PATCH | `/users/{id}` `{friendly_name, keep_history, max_streams, max_transcodes, limit_action, limit_message}` | Edits a user | write |
| POST | `/users/{id}/stop` `{message}` | Stops all their streams: `{stopped}` | write |
| GET | `/users/{id}/completion` | `{in_progress, finished, abandoned}` | read |
| GET | `/alerts?all&user_id&limit` | `{items, unacknowledged}` | read |
| POST | `/alerts/{id}/ack`, `/alerts/ack` | Acknowledge one, or all | write |
| GET | `/libraries`, `/libraries/{id}`, `/items/{rating_key}` | Libraries and items | read |
| GET | `/stats/summary`, `/stats/timeseries?group&split`, `/stats/by?dim`, `/stats/heatmap`, `/stats/breakdown?field`, `/stats/top?kind`, `/stats/concurrent?group`, `/stats/resolution` | The Graphs page's data, chart-ready | read |
| GET | `/insights/transcodes`, `/insights/transcodes/share`, `/insights/transcodes/reasons` | Transcodes: summary, reasons, by field, re-encode candidates | read |
| GET | `/insights/library-health?library_id&type&limit`, `/insights/collection?library_id&type&limit` | Library health and collection makeup | read |
| GET | `/insights/binges?range&user_id&limit`, `/insights/playback?range&limit` | Binges; playback quality | read |
| GET | `/insights/downloads`, `/insights/downloads/series?days`, `/insights/arr?limit` | SABnzbd totals and chart; Sonarr and Radarr summaries | read |

## Pins

Everything here is under `/api/v1/pins`, JSON only.

- **Auth:** as above. Reads work with any token; pinning and unpinning need a write one.
- **Errors:** `{"error": {"code", "message"}}`.
- **Whose pins:** every endpoint is about one person: `?user_id=<Plex user id>` (the id is in the address bar on their Users page), or the Plex server owner if left out. An unknown id is a 404.
- **Times:** `*_at` is UTC epoch seconds; `*_local` is the same time as ISO 8601 in Binge's timezone setting.

```bash
TOKEN=...   # Settings → API tokens
curl -H "Authorization: Bearer $TOKEN" https://binge.example.com/api/v1/pins/summary
```

| Method | Path | Answers |
|---|---|---|
| GET | `/pins` | Every pin: `tvdb_id`, `title`, `outlook`, `outlook_label`, `next_at`/`next_local`, `notify`, `in_sonarr` |
| PUT | `/pins/{tvdb_id}` | Pin a show (it must be in Plex or Sonarr): `{tvdb_id, pinned, changed}`. Write token |
| DELETE | `/pins/{tvdb_id}` | Unpin it. Write token |
| GET | `/pins/next` | The next pinned episode still to air, or `null` |
| GET | `/pins/schedule?days=7&back=1` | Pinned episodes from `back` days ago to `days` ahead (max 60 and 30), each with `code`, `state` (`available`, `on_disk`, `downloading`, `airing_today`, `upcoming`, `awaiting`, `missing`, `unmonitored`), `label`, `downloading_pct`, `stalled` |
| GET | `/pins/arrivals?hours=24` | Episodes of pinned shows that arrived in Plex in the last `hours`: the trigger an automation wants |
| GET | `/pins/ready` | Calendar → Ready: per show, the next episode after where you've got to and how many are waiting |
| GET | `/pins/retire` | Calendar → Retire: groups `finished`, `over`, `cold` |
| GET | `/pins/discover` | Calendar → Discover: unpinned shows you've watched with something coming |
| GET | `/pins/summary` | A dashboard's worth in one call (see below) |
| GET | `/pins/widget` | What the Scriptable widget draws (`docs/widget/`), worded here so phrasing can change without reinstalling it |
| GET | `/pins/poster/{tvdb_id}` | The show's poster (JPEG): Plex's, else Sonarr's |

**`/pins/summary`** is not RESTful on purpose. A wall display redrawing every minute makes one request, and every number in it comes from the same instant:

```json
{"generated_at": 1793880000, "user_id": 5342218, "healthy": true, "failing": [], "pins": 66,
 "next": {"show": "The Simpsons", "code": "S38E06", "air_local": "2026-11-09T01:00:00+00:00", "state": "upcoming", "...": "..."},
 "today": [], "overdue": {"total": 3, "missing": 1},
 "ready": {"shows": 44, "episodes": 312, "top": [{"show": "Major Crimes Unit", "next": "S01E03", "waiting": 3, "new": true}]},
 "downloads": {"jobs": 2, "stalled": 0}}
```

- **`healthy`** is false when the worker isn't running, or Sonarr, Radarr, SABnzbd or TMDB failed its last read (`failing` names which). It doesn't say whether Binge is up, since you just reached it; it says whether what it's telling you is fresh.
- **Home Assistant** already has `{name} next pinned` and `{name} pinned ready` over MQTT for tracked users (SPEC.md section 14), so it doesn't need this API.
