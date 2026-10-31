# Binge Stats — build spec

Read `DESIGN.md` first. This is the detail: layout, schema, ingest rules, the Tautulli import, each feature's logic, pages, API, deployment and milestones. Build milestone by milestone, and keep every milestone shippable on its own.

---

## 1. Repository layout

```
Binge-Stats/
  bingestats/
    __init__.py        create_app()
    __main__.py        python -m bingestats {web|worker|import-tautulli|create-admin|migrate}
    config.py          bootstrap config (/data/config.toml) + DB-backed settings
    db.py              connection helper, schema, migrations
    services/          all business operations; UI routes and the API both call these
      activity.py  history.py  stats.py  users.py  libraries.py  insights.py
      wrapped.py   notify.py   rules.py  settings.py  auth.py  jobs.py
    plex/
      client.py        HTTP client (token, timeouts, JSON, pagination)
      models.py        dataclasses: PlexSession, PlexItem, PlexUser, TranscodeInfo
      parse.py         Plex JSON -> models (all Plex field knowledge lives here)
      auth.py          plex.tv PIN sign-in flow for getting a token
      ws.py            websocket listener (nudge only)
    worker/
      main.py          scheduler loop
      poller.py        session state machine -> active_sessions -> plays
      library_sync.py  users_sync.py  geo.py  mqtt.py  backups.py
    importers/
      tautulli.py
    web/
      routes_ui.py  routes_api.py  routes_img.py  auth.py
    templates/
      base.html  macros/  pages/  partials/
    static/
      css/input.css  js/app.js  js/charts.js  vendor/
  tests/
    conftest.py  fixtures/plex/*.json  fixtures/tautulli_schema.sql  ...
  docs/
    verification.md
  docker/
    Dockerfile  entrypoint.sh
  docker-compose.yml
  scripts/fetch-assets.sh  scripts/build-css.sh
  config.example.toml
  requirements.txt  requirements-dev.txt
  CLAUDE.md  DESIGN.md  SPEC.md  README.md
```

Dependencies, with exact versions pinned in `requirements.txt`. Add each one in the milestone that first needs it:
- M1: `flask`, `gunicorn`, `requests`, `websocket-client`.
- Later: `apprise` (M4), `paho-mqtt` (M5), `maxminddb` (M3).
- Dev: `pytest`, `responses`, `pyflakes`.
- No Pillow: Plex's photo transcoder does the resizing.

---

## 2. Configuration

**Bootstrap** (`/data/config.toml`, created on first run with a generated `secret_key`):

```toml
secret_key = "generated"
data_dir = "/data"
log_level = "INFO"
trusted_proxies = []         # IPs/CIDRs allowed to set X-Forwarded-* and the auth header
```

**Everything else is a setting in the `settings` table**, edited in the UI. Keys and defaults:

| Key | Default | Notes |
|---|---|---|
| `plex.url` | — | e.g. `http://192.168.1.10:32400` |
| `plex.token` | — | from PIN sign-in or pasted |
| `plex.verify_tls` | `true` | |
| `poll.interval_s` | `5` | 2–30 |
| `poll.use_websocket` | `true` | |
| `ingest.min_play_s` | `30` | plays shorter than this are dropped |
| `ingest.grace_polls` | `3` | missed polls before a session is ended |
| `ingest.watched_pct.movie` | `85` | |
| `ingest.watched_pct.episode` | `85` | |
| `ingest.watched_pct.track` | `50` | |
| `ingest.group_resume_h` | `24` | window for grouping a resumed play |
| `binge.gap_min` | `30` | max gap between episodes in a binge |
| `binge.min_episodes` | `3` | |
| `abandoned.days` | `60` | |
| `library.sync_h` | `6` | |
| `library.stale_months` | `12` | "nobody watched in N months" |
| `geo.enabled` | `true` | |
| `ui.timezone` | `Europe/London` | |
| `ui.theme` | `dark` | `dark` / `light` / `system` |
| `mqtt.*` | off | `enabled, host, port, username, password, tls, base_topic=bingestats, discovery_prefix=homeassistant` |
| `backup.keep` | `7` | nightly backups kept |
| `auth.proxy_header` | empty | optional trusted-header login |

`bingestats/services/settings.py` exposes typed `get(key)` / `set(key, value)` with the defaults above in one dict. Nothing else reads the table directly.

---

## 3. Schema

All timestamps are integer UTC epoch seconds. All durations are integer seconds unless the name ends `_ms`. `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000` on every connection. `schema_version` holds a single integer, and `db.py` has an ordered list of migration functions. Each migration has a test that upgrades a database from the previous version.

```sql
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);   -- JSON-encoded

CREATE TABLE admin (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  username TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL
);

CREATE TABLE api_tokens (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  scope TEXT NOT NULL CHECK (scope IN ('read','write')),
  created_at INTEGER NOT NULL, last_used_at INTEGER
);

CREATE TABLE servers (
  id INTEGER PRIMARY KEY, machine_id TEXT UNIQUE, name TEXT, version TEXT,
  last_seen_at INTEGER, is_up INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY,               -- Plex account id (same id Tautulli uses)
  username TEXT, friendly_name TEXT,    -- friendly_name is admin-editable, shown everywhere
  email TEXT, thumb_url TEXT,
  is_owner INTEGER NOT NULL DEFAULT 0, is_home INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1, -- 0 once removed from Plex sharing; history kept
  keep_history INTEGER NOT NULL DEFAULT 1,
  max_streams INTEGER, max_transcodes INTEGER,          -- NULL = no limit
  limit_action TEXT NOT NULL DEFAULT 'notify' CHECK (limit_action IN ('notify','stop')),
  limit_message TEXT,
  share_salt TEXT NOT NULL,                              -- rotate to revoke Wrapped links
  first_seen_at INTEGER, last_seen_at INTEGER
);

CREATE TABLE libraries (
  id INTEGER PRIMARY KEY,               -- Plex section id
  server_id INTEGER NOT NULL REFERENCES servers(id),
  name TEXT NOT NULL, type TEXT NOT NULL, -- movie | show | artist | photo
  item_count INTEGER, child_count INTEGER, total_size_bytes INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1, synced_at INTEGER
);

-- Current library contents, refreshed by library sync. Keyed by rating_key but
-- linked to history by guid, because rating keys change when Plex re-adds an item.
CREATE TABLE media_items (
  rating_key INTEGER PRIMARY KEY,
  server_id INTEGER NOT NULL, library_id INTEGER REFERENCES libraries(id),
  type TEXT NOT NULL,                   -- movie | show | season | episode | artist | album | track
  guid TEXT,                            -- plex://movie/... ; stable across re-adds
  parent_rating_key INTEGER, grandparent_rating_key INTEGER,
  title TEXT NOT NULL, parent_title TEXT, grandparent_title TEXT,
  season_index INTEGER, episode_index INTEGER, year INTEGER,
  duration_s INTEGER, added_at INTEGER, originally_available_at TEXT,
  thumb TEXT, art TEXT,
  size_bytes INTEGER,                   -- sum of parts; NULL for containers
  video_codec TEXT, video_resolution TEXT, video_dynamic_range TEXT,
  audio_codec TEXT, audio_channels INTEGER, container TEXT, bitrate_kbps INTEGER,
  is_present INTEGER NOT NULL DEFAULT 1, -- 0 when gone from Plex
  synced_at INTEGER
);
CREATE INDEX media_items_guid ON media_items(guid);
CREATE INDEX media_items_parent ON media_items(parent_rating_key);
CREATE INDEX media_items_grandparent ON media_items(grandparent_rating_key);

-- In-flight sessions. Owned by the poller. One row per Plex Session.id.
CREATE TABLE active_sessions (
  session_id TEXT PRIMARY KEY,          -- Plex Session.id
  session_key TEXT, server_id INTEGER NOT NULL,
  user_id INTEGER, rating_key INTEGER, media_type TEXT,
  started_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL, missed_polls INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL,                  -- playing | paused | buffering
  state_since INTEGER NOT NULL,
  paused_s INTEGER NOT NULL DEFAULT 0,
  buffer_count INTEGER NOT NULL DEFAULT 0,
  start_offset_s INTEGER, view_offset_s INTEGER, duration_s INTEGER,
  snapshot TEXT NOT NULL                -- JSON of the latest parsed PlexSession
);

-- Finished plays. The core table.
CREATE TABLE plays (
  id INTEGER PRIMARY KEY,
  server_id INTEGER NOT NULL,
  group_id INTEGER REFERENCES plays(id),  -- first play of a resumed sequence; NULL = it is the first
  source TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live','tautulli')),
  source_id INTEGER,                      -- tautulli session_history.id when imported
  session_id TEXT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  started_at INTEGER NOT NULL, stopped_at INTEGER NOT NULL,
  paused_s INTEGER NOT NULL DEFAULT 0,
  play_s INTEGER NOT NULL,                -- stopped - started - paused, floored at 0
  start_offset_s INTEGER, view_offset_s INTEGER, duration_s INTEGER,
  percent_complete INTEGER, watched INTEGER NOT NULL DEFAULT 0,
  buffer_count INTEGER NOT NULL DEFAULT 0,
  -- what was played (snapshot at play time; media_items may change or vanish)
  media_type TEXT NOT NULL, rating_key INTEGER, parent_rating_key INTEGER, grandparent_rating_key INTEGER,
  guid TEXT, grandparent_guid TEXT, library_id INTEGER,
  title TEXT, parent_title TEXT, grandparent_title TEXT,
  season_index INTEGER, episode_index INTEGER, year INTEGER, thumb TEXT,
  -- where
  player_title TEXT, platform TEXT, product TEXT, device TEXT, machine_id TEXT,
  ip_address TEXT, is_local INTEGER, is_relayed INTEGER, is_secure INTEGER,
  -- how
  decision TEXT,                          -- direct_play | direct_stream | transcode
  video_decision TEXT, audio_decision TEXT, subtitle_decision TEXT,
  transcode_reasons TEXT,                 -- JSON array of reason codes (section 6)
  hw_decode INTEGER, hw_encode INTEGER,
  src_container TEXT, src_video_codec TEXT, src_resolution TEXT, src_dynamic_range TEXT,
  src_audio_codec TEXT, src_audio_channels INTEGER, src_bitrate_kbps INTEGER,
  dst_container TEXT, dst_video_codec TEXT, dst_resolution TEXT,
  dst_audio_codec TEXT, dst_audio_channels INTEGER, dst_bitrate_kbps INTEGER,
  subtitle_codec TEXT, bandwidth_kbps INTEGER, quality_profile TEXT,
  UNIQUE (source, source_id)
);
CREATE INDEX plays_user_started ON plays(user_id, started_at);
CREATE INDEX plays_started ON plays(started_at);
CREATE INDEX plays_guid ON plays(guid);
CREATE INDEX plays_grandparent_guid ON plays(grandparent_guid);
CREATE INDEX plays_rating_key ON plays(rating_key);
CREATE INDEX plays_library ON plays(library_id, started_at);
CREATE INDEX plays_group ON plays(group_id);

CREATE TABLE ip_geo (
  ip TEXT PRIMARY KEY, is_private INTEGER NOT NULL,
  city TEXT, region TEXT, country_code TEXT, country TEXT, lat REAL, lon REAL,
  looked_up_at INTEGER NOT NULL
);

-- Derived. Rebuildable from plays at any time (job: rebuild_binges).
CREATE TABLE binges (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, grandparent_guid TEXT NOT NULL,
  show_title TEXT, started_at INTEGER NOT NULL, ended_at INTEGER NOT NULL,
  episode_count INTEGER NOT NULL, play_s INTEGER NOT NULL,
  first_play_id INTEGER NOT NULL, last_play_id INTEGER NOT NULL
);
CREATE INDEX binges_user ON binges(user_id, started_at);

CREATE TABLE alerts (                   -- sharing / limits / server events shown in the UI
  id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL,
  kind TEXT NOT NULL,                   -- concurrent_ips | new_country | stream_limit | transcode_limit | server_down | server_up
  user_id INTEGER, detail TEXT NOT NULL,-- JSON
  acknowledged INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE notify_channels (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, apprise_url TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE notify_rules (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  trigger TEXT NOT NULL, conditions TEXT NOT NULL DEFAULT '[]',  -- JSON (section 9)
  title_template TEXT, body_template TEXT NOT NULL,
  channel_ids TEXT NOT NULL,            -- JSON array
  cooldown_s INTEGER NOT NULL DEFAULT 0, include_poster INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE notify_log (
  id INTEGER PRIMARY KEY, rule_id INTEGER, channel_id INTEGER, sent_at INTEGER NOT NULL,
  dedupe_key TEXT, ok INTEGER NOT NULL, error TEXT, title TEXT, body TEXT
);
CREATE INDEX notify_log_dedupe ON notify_log(rule_id, dedupe_key, sent_at);

CREATE TABLE recently_added_queue (
  rating_key INTEGER PRIMARY KEY, queued_at INTEGER NOT NULL, processed_at INTEGER
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
  progress INTEGER, message TEXT, result TEXT,
  created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER
);

CREATE TABLE wrapped_cache (
  user_id INTEGER NOT NULL,             -- 0 = whole server
  year INTEGER NOT NULL, computed_at INTEGER NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY (user_id, year)
);
```

---

## 4. Plex client (`bingestats/plex/`)

All Plex knowledge lives here. Everything outside this package works with the dataclasses in `models.py`.

- Requests send `X-Plex-Token`, `Accept: application/json`, `X-Plex-Client-Identifier` (a stable UUID stored in settings), `X-Plex-Product: Binge Stats`. The timeout is 10 s, and there is one retry on connection error.
- Paginated library calls use `X-Plex-Container-Start` / `X-Plex-Container-Size` (page size 500).

| Use | Endpoint |
|---|---|
| Server identity | `GET /identity`, `GET /` |
| Sessions | `GET /status/sessions` |
| Stop a stream | `GET /status/sessions/terminate?sessionId={Session.id}&reason={msg}` |
| Libraries | `GET /library/sections` |
| Library items | `GET /library/sections/{id}/all?type={1 movie, 2 show, 4 episode, 8 artist, 10 track}&includeGuids=1` |
| Item metadata | `GET /library/metadata/{rating_key}` |
| Recently added | `GET /library/recentlyAdded` |
| Images | `GET /photo/:/transcode?width=&height=&url={thumb}` (or the raw `thumb` path) |
| Websocket | `ws(s)://{host}/:/websockets/notifications?X-Plex-Token=` |
| Owner | `GET https://plex.tv/api/v2/user` |
| Shared users | `GET https://plex.tv/api/users` (XML), plus users seen in sessions |
| Token sign-in | `POST https://plex.tv/api/v2/pins?strong=true`, then poll `GET /api/v2/pins/{id}` |

**Verify against live:**
- the exact JSON shape of `/status/sessions` for a direct play, a direct stream, a video transcode and a subtitle burn, and for music. Save sanitised copies as `tests/fixtures/plex/*.json`, with tokens, IPs and names replaced.
- whether `plex.tv/api/v2/friends` returns everything `api/users` does, so we can stay JSON-only.

### Parsed session (`PlexSession`)

From each `Metadata` entry in `/status/sessions`:
- **identity:** `session_id` (Session.id), `session_key`, `user_id`, `user_title`, `user_thumb`.
- **item:** `rating_key`, `parent_rating_key`, `grandparent_rating_key`, `guid`, `grandparent_guid`, `media_type`, `title`, `parent_title`, `grandparent_title`, `season_index`, `episode_index`, `year`, `thumb`, `library_id`, `duration_s`, `view_offset_s`.
- **player:** `player_title`, `platform`, `product`, `device`, `machine_id`, `state`, `ip_address` (Player.address, or Player.remotePublicAddress when not local), `is_local`, `is_relayed`, `is_secure`, `bandwidth_kbps`, `location`.
- **media:** `TranscodeInfo` (section 6).

---

## 5. Ingest: session state machine (`worker/poller.py`)

Runs every `poll.interval_s`, and immediately on a websocket `playing` notification (debounced to 1 s). Each poll does:

1. Fetch `/status/sessions`. On failure:
   - mark the server down after 3 consecutive failures, and raise a `server_down` alert and trigger;
   - **do not** count missed polls for sessions while the server is unreachable;
   - mark it up again (`server_up`) on the first success.
2. For each session in the response, keyed by `session_id`:
   - **New:** insert into `active_sessions` with `started_at = now`, `state_since = now` and `start_offset_s = view_offset_s`. Fire `play_start`. Run limit and sharing checks (section 10).
   - **Existing:**
     - Update `last_seen_at`, set `missed_polls = 0`, update the offsets and the snapshot.
     - **State change:** when leaving `paused`, add `now - state_since` to `paused_s`; when entering `buffering`, increment `buffer_count`. Set `state_since = now`. Fire `pause`, `resume` or `buffer` (buffer fires when `buffer_count` reaches 3 within 5 minutes).
     - **Item change on the same session** (autoplay without a new Session.id): finalise the old play, then treat the row as new.
     - **Decision change** (e.g. direct play → transcode after a quality change): keep the latest in the snapshot and fire `transcode_start` if it went to transcode.
     - **Crossed the watched threshold:** fire `watched` once (`fired_watched` is stored in the snapshot).
3. For each `active_sessions` row not in the response, increment `missed_polls`. When it reaches `ingest.grace_polls`, finalise it with `stopped_at = last_seen_at`. A user who paused and closed the app gets their paused time counted up to the point it disappeared, not to when we noticed.
4. **Finalising a play:**
   - `play_s = max(0, stopped_at - started_at - paused_s)`, adding any open pause first.
   - Drop the play if `play_s < ingest.min_play_s`, or if the user has `keep_history = 0`.
   - `percent_complete = round(view_offset_s / duration_s * 100)`. `watched = percent_complete >= watched_pct[media_type]`.
   - **Group resumes:** find the user's most recent play of the same `guid` (or the same `rating_key` if there is no guid) that stopped within `ingest.group_resume_h` and wasn't watched, and whose `view_offset_s` is within 120 s of this play's `start_offset_s`. Set `group_id` to that play's `group_id` or, if it has none, its `id`.
   - Insert into `plays`, delete from `active_sessions`, fire `play_stop`, update the binge for that user (section 11) and queue an IP geo lookup.
5. On worker start, any `active_sessions` rows left from before a restart are kept and reconciled on the first successful poll. Rows whose sessions are gone are finalised with their stored `last_seen_at`.

The poller takes `now` from an injected clock so tests can drive the whole state machine with fixture responses and frozen time. Each rule above gets a test.

---

## 6. Transcode decision and inferred reasons

Plex's sessions API reports the decisions but not the reason, so we infer one. Name and label the reasons as "inferred" in the UI.

- `decision`:
  - `transcode` if `videoDecision == transcode` or `audioDecision == transcode`, or if `subtitleDecision == burn`;
  - otherwise `direct_stream` if a `TranscodeSession` exists (container or stream copy);
  - otherwise `direct_play`.
- `transcode_reasons` is a list of codes, any of which can apply:

| Code | Rule |
|---|---|
| `subtitle_burn` | `subtitleDecision == burn` |
| `video_codec` | video transcode and source codec ≠ target codec |
| `video_resolution` | video transcode and target height < source height (usually a quality / bandwidth setting) |
| `video_bitrate` | video transcode, same codec and resolution, lower bitrate |
| `hdr_tonemap` | source dynamic range is HDR/DV and target is SDR |
| `audio_codec` | audio transcode and source codec ≠ target codec |
| `audio_channels` | audio transcode and fewer target channels |
| `container` | direct stream, container differs |
| `transcoder_slow` | video transcode, `TranscodeSession.speed` < 1.0 and not `throttled` (the transcoder can't keep up). A flag, not a cause |

- `hw_decode` / `hw_encode` come from `transcodeHwDecoding` / `transcodeHwEncoding` (or `transcodeHwFullPipeline`). **Verify against live** which fields a current PMS sets.

---

## 7. Library and user sync

- **Users** every 6 h and at startup:
  - Upsert the owner and shared users. Users seen in sessions but missing from plex.tv get a stub row.
  - Users removed from sharing get `is_active = 0`. They are never deleted.
- **Libraries** every `library.sync_h` hours, and on demand from a job:
  - Upsert sections.
  - For each movie, show and music library, page through items (episodes and tracks via `type=4` / `type=10`) and upsert into `media_items`, taking size, codecs, resolution and bitrate from the first `Media`/`Part`.
  - Mark items not seen this sync `is_present = 0`. Never delete them, because history links to them.
  - Update the library totals.
- **Recently added:** compare `added_at` against the last sync. New movies, episodes and albums go into `recently_added_queue`. The notifier (section 9) drains the queue.
- **Linking history to items:** use `guid` first, then `rating_key`. A play keeps its own snapshot fields, so a missing item never breaks a history row.

---

## 8. Tautulli import (`importers/tautulli.py`)

Entry points: `python -m bingestats import-tautulli /import/tautulli.db [--dry-run]`, or Settings → Import (a path inside the container, so mount Tautulli's config folder read-only at `/import`). Both create a `tautulli_import` job.

**Steps:**
1. Copy the database to a temp file using the SQLite backup API, so a live Tautulli with WAL files is safe to read. Open the copy read-only.
2. Check that the schema has the tables and columns we need, and fail with a clear message naming anything missing.
3. **Dry run:** count users, plays, the date range, plays already imported (`source='tautulli'` with a matching `source_id`), and plays whose user is missing from `users`. Show this as the confirmation screen.
4. Import in batches of 2,000 in one transaction per batch, updating the job progress as it goes:
   - **Users:** `users.user_id → users.id`, `username`, `friendly_name`, `email`, `thumb`, `is_admin → is_owner`, `is_home_user → is_home`, `keep_history`, `deleted_user → is_active = 0`. Never overwrite a `friendly_name` that was already set here.
   - **Plays:** `session_history` joined to `session_history_metadata` and `session_history_media_info` on `id`.
     - **Times:** `started_at = started`, `stopped_at = stopped`, `paused_s = paused_counter`, `play_s = stopped - started - paused_counter` (floored at 0).
     - **Offsets:** `view_offset_s = view_offset / 1000`, `duration_s = metadata.duration / 1000`, then compute `percent_complete` and `watched` with our thresholds.
     - **Item:** `rating_key`, `parent_rating_key`, `grandparent_rating_key`, `media_type`, `title`, `parent_title`, `grandparent_title`, `media_index → episode_index`, `parent_media_index → season_index`, `year`, `thumb`, `guid`, `section_id → library_id`.
     - **Player:** `player → player_title`, `platform`, `product`, `machine_id`, `ip_address`, `location → is_local` (`lan`), `relayed`, `secure`, `bandwidth`, `quality_profile`.
     - **Media info:**
       - decisions from `transcode_decision` (`direct play` / `copy` / `transcode` mapped to our three values), `video_decision`, `audio_decision`, `stream_subtitle_decision`;
       - source fields from `container`, `video_codec`, `video_resolution`, `video_dynamic_range`, `audio_codec`, `audio_channels`, `bitrate`;
       - target fields from the `stream_*` columns;
       - `transcode_hw_decoding`, `transcode_hw_encoding`.
     - Compute `transcode_reasons` with the section 6 rules from these columns.
     - `source = 'tautulli'`, `source_id = session_history.id`. Insert or ignore on the unique key, so the import is idempotent.
   - **Grouping:** Tautulli's `reference_id` points at the first row of a resume group. After the insert, set `group_id` to the play whose `source_id = reference_id` (NULL when that's itself).
   - **`grandparent_guid` for episodes:** filled from `media_items` by `grandparent_rating_key` when it's present, otherwise from `metadata.grandparent_guid` if the column exists. Report the count still missing. Binges and completion fall back to `grandparent_rating_key` for those plays.
5. Queue `rebuild_binges` and IP geo lookups for new IPs. Clear `wrapped_cache`.
6. **Result:** counts imported, skipped (duplicates), skipped (user filtered) and failed, with reasons. Show it on the job page.

**Verify against live:** column names and units against Marc's actual `tautulli.db`. In particular, check whether `duration` is in ms, whether `guid` holds `plex://` GUIDs or legacy agent GUIDs, and the `transcode_decision` values. Write the findings in `docs/verification.md`, and build `tests/fixtures/tautulli_schema.sql` from the real `.schema` output, with no data.

Overlap with live data: if a Tautulli play overlaps a live play for the same user and `rating_key` (start within 60 s), skip the Tautulli play and count it as a duplicate. That covers running both side by side before switching off Tautulli.

---

## 9. Notifications (`services/notify.py`, `services/rules.py`)

**Triggers**, each with a context dict:

| Trigger | Context |
|---|---|
| `play_start` `play_stop` `pause` `resume` `buffer` `watched` `transcode_start` | session/play fields, `user`, `item`, `stream` (decision, reasons, quality), `location` |
| `recently_added` | `items` (grouped), `library`. Batched: wait 10 min after the last addition, then one message per show/season or per movie list |
| `stream_limit` `transcode_limit` `concurrent_ips` `new_country` | `user`, `sessions`, `detail` |
| `server_down` `server_up` | `server`, `down_for_s` |

**Conditions:** a JSON list, all of which must match (AND). Each one is `{"field": "user.id", "op": "in", "value": [123, 456]}`.
- Ops: `eq`, `ne`, `in`, `not_in`, `contains`, `gt`, `lt`, `is_true`, `is_false`.
- Fields come from a fixed whitelist per trigger, which the UI uses to build dropdowns.
- No OR. If OR is needed, write two rules.

**Templates** are Jinja in a `SandboxedEnvironment`, with a preview button that renders against the last real event of that trigger, or a sample event when there isn't one. There are helpers for `duration` and `progress_bar`.

**Delivery:** each channel is an Apprise URL. Send in a worker thread with a 15 s timeout. Attach the poster from the image cache when `include_poster` is set and the channel supports attachments.

**Cooldown and dedupe:** `dedupe_key` is per trigger (for example `play_start:{session_id}` or `stream_limit:{user_id}`). Skip a send if the same rule and key were sent within `cooldown_s`. Log every attempt in `notify_log`, and add a "test channel" button.

**Shipped default rules** (disabled until a channel is picked): "Someone started watching", "Transcode because of subtitles", "Stream limit hit", "Server down", "Recently added digest".

---

## 10. Stream limits and sharing detection

Checked when a session starts:
- **Stream limit:** count the user's active sessions. If the count is over `max_streams`, raise a `stream_limit` alert and trigger. If `limit_action = 'stop'`, stop the **newest** session with `limit_message`.
- **Transcode limit:** same as the stream limit, counting only transcoding sessions.
- **Concurrent IPs:** two or more active sessions for one user from different **public** IPs (private and LAN IPs are ignored). Raise a `concurrent_ips` alert with both locations. This flags only and never stops anything automatically.
- **New country:** the first play ever for this user from a country (by geo) that isn't the user's most common country. Flag only.
- **Alerts** appear on the dashboard with an "acknowledge" action and live on the user's page.

---

## 11. Binges, completion, abandoned

- **Binge (materialised in `binges`):**
  - After an episode play is finalised, look at the user's previous episode play.
  - If it is the same show (`grandparent_guid`, falling back to `grandparent_rating_key`) and `started_at - previous.stopped_at <= binge.gap_min`, extend the binge that ended with the previous play, or start a new candidate run.
  - A run counts as a binge once it reaches `binge.min_episodes`. Grouped resumes count as one episode.
  - `rebuild_binges` recomputes the whole table from `plays` with the same function, which has a test.
- **Completion:** per user per show, distinct watched episodes (by guid) ÷ episodes present in `media_items` for that show. Computed on query and cached for 10 min.
- **Abandoned:** at least one watched episode, completion below 100 %, no play of the show for `abandoned.days`, and the show still present in the library.

---

## 12. Insights

**Transcodes page:**
- share of plays by decision over time;
- reasons ranked;
- by user, by platform/product (so you can see which client causes it), by source codec/resolution;
- **re-encode candidates:** items with ≥ 3 plays where ≥ 50 % transcoded for a file reason (`video_codec`, `hdr_tonemap`, `audio_codec`, `subtitle_burn`, `container`), ranked by transcoded play count, showing the file's codecs.

**Library health page**, with filters for library and type:
- **Stale:** present items with no play (by guid) in `library.stale_months`, and added before that window. Shows size, added date, last played and by whom.
- **Never watched:** added more than 90 days ago with no plays ever.
- **Storage per watched hour:** for each show (summed over episodes) and movie, `size_gb / total_play_hours`, ranked highest first. Items with no plays show "∞" and sort to the top.
- **Totals banner:** "X TB in stale content across N items".
- Export to CSV.

---

## 13. Wrapped

Per user and per year, plus server-wide (`user_id = 0`). Computed by `services/wrapped.py`, cached in `wrapped_cache`, and recomputed if older than 1 day while the year is still running.

**Contents:**
- total watch time, with a fun comparison ("that's 3.2 trips London → Sydney");
- plays, distinct titles;
- top 5 shows and top 5 movies with posters;
- longest binge (show, episodes, date);
- busiest day;
- viewing heatmap (hour × weekday);
- month-by-month bars;
- favourite device;
- the % of plays that direct played;
- first and last play of the year;
- a "you finished N shows" list (completion reached 100 % that year);
- percentile among server users ("top 10 % of viewers on this server").

**Page:**
- full-bleed, scrolling sections with big type and poster backdrops;
- works on a phone;
- no app chrome on the public share version;
- a print stylesheet that gives a tidy one-page summary.

**Share link:** `/wrapped/s/<token>`, where the token is `itsdangerous.URLSafeSerializer(secret_key, salt=user.share_salt)` over `{"u": user_id, "y": year}`. "Revoke links" on the user page rotates `share_salt`. The public route serves only that page, plus images through a proxy that is scoped to the token.

---

## 14. Home Assistant (MQTT)

Off until configured. The worker publishes discovery configs under `{discovery_prefix}` with one device, "Binge Stats" (model: the Plex server name). State is published under `{base_topic}/...`, retained, with availability on `{base_topic}/status` via LWT.

| Entity | Type | Value |
|---|---|---|
| Active streams | sensor | count, with attributes listing sessions (user, title, state, decision) |
| Transcodes | sensor | count |
| Direct plays / direct streams | sensor | counts |
| Bandwidth total / LAN / WAN | sensor | Mbps, 1 dp |
| Plex server | binary_sensor (connectivity) | up/down |
| Plays today / watch time today | sensor | count / hours |
| Unacknowledged alerts | sensor | count |
| `{friendly_name}` watching | sensor, per tracked user | the title, or "idle"; attributes: progress %, device, decision |
| Stop `{friendly_name}`'s streams | button, per tracked user | stops all of that user's sessions with the default message |

- **Tracked users** are chosen in Settings → Home Assistant. The default is none, so we don't create entities for every user.
- Publish on change, and every 60 s.
- Command topic: `{base_topic}/cmd/stop_user/{user_id}`.
- Removing a tracked user publishes an empty discovery config so HA deletes the entity.

---

## 15. Pages

Every page is a Jinja template that extends `base.html`. Pages refresh their parts with HTMX partials; there are no full-page JS apps. Routes are thin, and all data comes from `services/`.

| Route | Page |
|---|---|
| `/setup` | First-run wizard: create the admin → connect Plex (PIN sign-in, or URL + token) → choose libraries → optional Tautulli import → done |
| `/login` | |
| `/` | **Dashboard.** Now-playing strip (compact session cards), today / 7d / 30d tiles (plays, watch time, users, peak concurrent), plays-per-day chart (stacked by media type), top 5 users / shows / movies for 30 days, unacknowledged alerts, recently added row |
| `/activity` | **Activity.** Full session cards (poster, progress, user avatar, device, location, quality source → target, decision badge with reasons tooltip, bandwidth, stop button), totals bar, "nothing playing" empty state |
| `/history` | **History.** Filter bar (user, library, type, decision, platform, IP, date range, search). Table with poster thumb, title, user, started, duration, % bar, decision. Grouped resumes collapse into one row. 50 per page, paging by keyset on `started_at, id`, CSV export of the current filter |
| `/users` | User cards / table: last seen, plays, watch time, last watched, active flag |
| `/users/<id>` | **User.** Header with avatar + friendly name (editable) + limits. Tiles, watch time over time, heatmap, top content, devices, IPs on a map (Leaflet + OSM tiles; vendored, tiles from OSM), binges, completion and abandoned shows, alerts, recent history, Wrapped link |
| `/libraries` | Library cards: item counts, size, plays 30d |
| `/libraries/<id>` | Library stats, most watched, recently added, health summary link |
| `/item/<rating_key>` | **Item.** Movie, show, season or episode with backdrop, metadata, plays over time, who watched (with completion for shows), history list. Falls back to the history snapshot if the item is gone from Plex |
| `/graphs` | Full chart gallery with a date range picker: plays/time by day, by weekday, by hour, by month, by platform, by product, by stream decision, by resolution source vs target, concurrent streams over time, top users stacked |
| `/insights/transcodes` | Section 12 |
| `/insights/library-health` | Section 12 |
| `/insights/binges` | Longest binges, binges by show, binge leaderboard |
| `/alerts` | Alert list, filter by kind, acknowledge |
| `/wrapped` | Year picker, server-wide Wrapped, list of users with links |
| `/wrapped/<user_id>/<year>` | Admin view |
| `/wrapped/s/<token>` | Public share (no auth) |
| `/notifications` | Rules list, rule editor (trigger → conditions → template with live preview → channels), channels, delivery log |
| `/settings` | Tabs: Plex, General (timezone, theme, thresholds), Home Assistant, Geo, Import, API tokens, Backups, About (version, DB size, worker heartbeat) |
| `/jobs/<id>` | Job progress, HTMX-polled |

**Worker heartbeat:** the worker writes `worker.heartbeat_at` to settings every poll. If it is older than 60 s, the UI shows a banner saying the worker isn't running.

**Images:**
- `/img/plex?path=<plex artwork path>&w=&shape=poster|art|square` takes a Plex artwork path. Only `/library/metadata/<n>/(thumb|art|…)/<n>` is accepted, and anything else is a 404.
- `/img/user/<id>` serves a user avatar, fetched from plex.tv (other hosts are refused).
- The image is fetched through the Plex photo transcoder at a width snapped to 150, 300, 600 or 1200 px, cached as JPEG in `/data/cache/img/` and served with a week-long private cache.

---

## 16. REST API

`/api/v1/`, JSON only, `Authorization: Bearer <token>`. Errors look like `{"error": {"code", "message"}}`. List endpoints take `limit` (max 500) and `cursor`. A UI session cookie also works, so the UI can use it for charts.

| Method | Path | Scope |
|---|---|---|
| GET | `/health` | none (no auth): `{ok, worker_alive, plex_up, version}` |
| GET | `/activity` | read |
| POST | `/activity/{session_id}/stop` `{message}` | write |
| GET | `/history?user_id&library_id&type&decision&from&to&q` | read |
| GET | `/users`, `/users/{id}`, `/users/{id}/stats?from&to` | read |
| PATCH | `/users/{id}` `{friendly_name, max_streams, max_transcodes, limit_action, limit_message, keep_history}` | write |
| GET | `/libraries`, `/libraries/{id}` | read |
| GET | `/items/{rating_key}` | read |
| GET | `/stats/summary?from&to` | read |
| GET | `/stats/timeseries?metric=plays\|watch_time&group=day\|week\|month&split=media_type\|user\|decision&from&to` | read |
| GET | `/stats/top?kind=users\|shows\|movies\|platforms&from&to&limit` | read |
| GET | `/stats/heatmap?user_id&from&to` | read |
| GET | `/insights/transcodes`, `/insights/library-health`, `/insights/binges` | read |
| GET | `/alerts`, POST `/alerts/{id}/ack` | read / write |
| GET | `/wrapped/{user_id}/{year}` | read |
| POST | `/jobs` `{kind, params}`, GET `/jobs/{id}` | write / read |

Document it in `docs/API.md`, with an example per endpoint.

---

## 17. Deployment

**`docker/Dockerfile`** (multi-stage):
1. Stage `assets`: `scripts/fetch-assets.sh` downloads htmx, Alpine and the Tailwind standalone CLI at the versions and sha256s in `bingestats/static/vendor/VERSIONS`. `scripts/build-css.sh` then builds `static/css/app.css`. The downloaded files are gitignored, and nothing loads from a CDN at runtime.
2. Stage `app`: `python:3.12-slim`, install the requirements, copy the app and the built CSS, run as a non-root user with UID/GID from `PUID`/`PGID` (default 1000).

**`docker-compose.yml`** (repo root, builds the image locally. GHCR comes in M8):

```yaml
services:
  web:
    image: ghcr.io/marctew/binge-stats:latest
    build: { context: .., dockerfile: docker/Dockerfile }
    command: web
    ports: ["8181:8181"]
    volumes: ["./data:/data"]
    environment: { TZ: Europe/London, PUID: 1000, PGID: 1000 }
    restart: unless-stopped
    healthcheck: { test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8181/api/v1/health')"], interval: 30s }
  worker:
    image: ghcr.io/marctew/binge-stats:latest
    command: worker
    volumes: ["./data:/data", "/path/to/tautulli/config:/import:ro"]
    environment: { TZ: Europe/London, PUID: 1000, PGID: 1000 }
    restart: unless-stopped
    depends_on: [web]
```

- `web` runs `gunicorn -k gthread -w 2 --threads 8 -b 0.0.0.0:8181 'bingestats:create_app()'`.
- `worker` runs `python -m bingestats worker`.
- Both run migrations at startup, guarded by a file lock in `/data` so only one migrates.
- **GeoIP:** the DB-IP Lite City MMDB (CC BY 4.0, no account needed) is downloaded by the worker to `/data/geo/` on the first run and monthly after that. Show an attribution line on pages that show locations. Lookups are cached in `ip_geo` for 30 days. Private ranges are stored as `is_private = 1`, labelled "LAN", and get no lookup.
- **Backups:** nightly at 03:30 local, using the SQLite backup API → `/data/backups/bingestats-YYYYMMDD.db`, keeping `backup.keep`. Settings has a "Backup now" button and lists the backups.
- **CI** (GitHub Actions): pytest on push. On a tag, build and push the image to GHCR.

**Development without Docker:** `python -m bingestats web --dev` (Flask debug server on 8181) and `python -m bingestats worker` in another shell, with `DATA_DIR=./data`. Run `scripts/build-css.sh` once after cloning. It fetches the vendored JS and the pinned Tailwind CLI (into `.cache/`) and builds the CSS. `--watch` rebuilds on template changes. The built CSS and fetched JS are gitignored.

---

## 18. Testing

- `python -m pytest` from the repo root.
- **Fixtures:**
  - a `db` fixture (temp SQLite with the schema);
  - an `app` / `client` fixture that is logged in;
  - a `plex` fixture using `responses` that serves `tests/fixtures/plex/*.json`;
  - a `clock` fixture for the poller.
- **Poller:** a test for every rule in section 5 (new session, pause accounting, buffering, item change, grace polls, server-down suppression, min play length, watched threshold, resume grouping, restart reconciliation).
- **Transcode reasons:** a table-driven test per row in section 6.
- **Tautulli import:** builds a small `tautulli.db` from `tests/fixtures/tautulli_schema.sql` with hand-written rows. It tests the mapping, grouping, idempotency (run twice, same counts), the overlap skip and the dry-run counts.
- **Binges:** incremental vs `rebuild_binges` give the same result.
- **Rules:** condition ops, template sandboxing (no attribute access to internals), cooldown and dedupe.
- **Migrations:** upgrade from each previous schema version.
- **Routes:** each page renders with an empty DB and with seeded data (status 200, no template errors).

---

## 19. Milestones

Each one ends with tests passing, the image building, and something Marc can run against his real Plex.

| # | Milestone | Done when |
|---|---|---|
| M1 | **Skeleton and live ingest.** Package layout, config, schema + migrations, admin auth, setup wizard (PIN + manual token), Plex client and parser, poller with the full state machine, websocket nudge, worker heartbeat, base layout and component macros, Activity page, plain History page, image proxy, compose + Dockerfile | Plays on the real server show live on Activity and land in History correctly after stop, including pause time and resume grouping. Fixtures captured from the live server |
| M2 | **Tautulli import, users and libraries.** Importer with dry run and job UI, user sync, library + media item sync, Users and Libraries pages (basic), item page | Marc's full Tautulli history imported. Counts match Tautulli's own totals per user within duplicates. Verification notes written |
| M3 | **Dashboard, stats and graphs.** Stats services, timeseries/top/heatmap, ECharts theme, Dashboard, Graphs, full User and Library pages, geo lookups + map, History filters + CSV, REST API read endpoints + tokens | The dashboard and graphs look finished on desktop and phone, and the numbers match a hand SQL check |
| M4 | **Notifications.** Apprise channels, rules engine and editor with preview, all triggers, recently-added digest, default rules, log | A Discord/ntfy message arrives for each trigger. A season drop sends one digest |
| M5 | **Control and Home Assistant.** Stop stream (UI, API, HA), stream/transcode limits, sharing detection, alerts page, MQTT discovery + state + commands | HA shows the entities, the stop button works, and a limit stops the newest stream |
| M6 | **Insights.** Binges (incremental + rebuild), completion, abandoned, Transcodes page, Library health page + CSV | The re-encode and stale lists make sense against the real library |
| M7 | **Wrapped.** Wrapped service + cache, admin and public pages, share links + revoke, print style | A shared link opens on a phone and looks good, and revoke kills it |
| M8 | **Polish.** Light theme pass, empty/loading states everywhere, backups UI, About page, README with screenshots, GHCR publish | Fresh install → setup → import → useful within 10 minutes |
