# REST API: pins

The rest of the API is in SPEC.md section 16. Everything here is under `/api/v1/pins`, JSON only.

- **Auth:** `Authorization: Bearer <token>`. Tokens are made in Settings → API tokens. Reads work with any token; pinning and unpinning need a read-write one.
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
