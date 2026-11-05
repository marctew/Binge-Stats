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
