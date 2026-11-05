# REST API

Binge has two kinds of route under `/api/v1/`:

- **The API standard's routes** (Design Uniformity `API-STANDARD.md`, draft 2; plan in `docs/specs/api-standard.md`): the same shape in every one of Marc's apps, for displays, Docket, Home Assistant and TRMNL. Errors are `{"error": "<sentence>", "code": "<code>"}`.
- **Binge's own routes** (the pins section below, and SPEC.md section 16), kept exactly as they were for the widget, the PC panel and the dashboard. Errors are `{"error": {"code", "message"}}`.

## Tokens, for every route

- `Authorization: Bearer <token>`, from Settings → API tokens. Read tokens read; write tokens read and write.
- **Read tokens may go in the URL** as `?token=…`, for displays that can't set a header. A write token in the URL is refused with `400`, even on a GET. URL tokens never reach the access log: gunicorn logs the path without the query string, and no referrer.
- A signed-in admin can read without a token (that's how the dashboard's charts load). Viewer logins can't use the API at all.
- **Limits:** 10 bad tokens from one address in a minute locks that address out for 15 minutes (`429` with `Retry-After`). Each token gets 120 requests a minute, then `429`. Both are counted per gunicorn worker, so with two workers they're about twice as loose; that's accepted (API standard section 10).
- **Through the tunnel:** `API_PUBLIC: "0"` in the compose file refuses any `/api/v1/` request that came through Cloudflare (`CF-Connecting-IP` set) with `403`. The default, `1`, answers there too, always with a token.
- **Caching:** every successful GET has a weak `ETag`; send it back as `If-None-Match` and an unchanged answer is `304` with no body.
- `/api/v1/health` needs no token.

## The standard's routes

| Method | Path | Answers |
|---|---|---|
| GET | `/meta` | Who Binge is and what it offers: `app`, `name`, `version`, `sha`, `api` (1), `accent`, `icon`, `time_zone`, and the lists `feeds`, `actions`, `displays`, `events` (empty until later stages) |
| GET | `/status` | The one-glance summary, below. `Cache-Control: private, max-age=7` |
| GET | `/feeds/{name}` | A list in the common item shape, below |
| GET | `/images/{kind}/{id}` | Artwork, resized here: `poster/{rating_key}`, `art/{rating_key}`, `show/{tvdb_id}` |

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

# Binge's own routes: pins

The rest of Binge's own API is in SPEC.md section 16. Everything here is under `/api/v1/pins`, JSON only.

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
