# Spec: pins and the pinned calendar (Pinnarr folded into Binge Stats)

Status: agreed 2026-11-05. **P1 built 2026-11-05** (pins, calendar, episode state, guide sync, `/me` versions). P2–P5 not built.

Changes from the plan below, made while building P1:
- **Guides are read on every Sonarr sync** (every `arr.sync_h`, 3 h), not nightly: it's one request per pinned show. A new pin queues a `pin_guides` job so the back catalogue shows within seconds. If a `pin_guides` job is already running when another show is pinned, that show waits for the next sync (the calendar window still covers it from −7 days).
- **One more state, `on_disk`** ("Downloaded"): Sonarr has the file but Plex doesn't have the episode yet (not scanned, or the library path is wrong). Without it those episodes would read as missing.
- **State order follows Pinnarr's code**, which differed from the table that was here: `airing_today` comes before `upcoming` and covers an episode that aired earlier today. See the table below.
- **"Aired, not arrived"** lists everything that aired before today, within 30 days, and isn't in Plex or unmonitored: expected, missing, downloading and on disk.
- **Pinning is from a search box on the calendar** (Plex and Sonarr shows by title) and from show, season and episode pages (admin). The shows browser in P2 adds the rest. Viewers can pin Sonarr-only shows, so `img.arr_poster` is on the viewer allow-list (posters only).
- **The web process writes `pins`**, added to its list of writes in DESIGN.md section 3 and CLAUDE.md.
- **Calendar** is in the sidebar (admin and viewers) only while Sonarr is set up.

## Why

Pinnarr (`github.com/marctew/pinnarr`) is a separate app. You pin the handful of shows you care about, and it gives you a calendar of just those, honest episode states ("aired 3 days ago and still not here"), a season outlook (dated, announced, filming, hiatus, dormant, cancelled, ended) and a push when an episode is actually in Plex.

Binge Stats already holds most of what Pinnarr has to sync for itself:
- the library and when episodes arrive in Plex (the 10-minute recently-added check);
- who has watched what (`plays`, `services/completion.py`);
- the Sonarr snapshot, the Sonarr queue, users, films, API tokens, backups and `/healthz`.

What Pinnarr adds is a layer on top: pins, the calendar, episode states, the outlook, and pin-scoped lists and notifications. This spec brings that layer into Binge so Pinnarr can be retired.

## Decisions

| Decision | Detail |
|---|---|
| **Binge stays read-only** (Marc, 2026-11-05) | Pins live only in Binge. These Pinnarr features are **not ported**: Sonarr tag two-way sync, Plex Watchlist sync, marking watched in Plex, and Overseerr requests. DESIGN.md section 1's read-only rule and section 3's limits on Plex calls are unchanged. |
| **TMDB, GET only** | A new outside service, needed for the outlook (cancelled vs ended vs in production). Optional: without a key, the outlook uses Sonarr alone and never says `cancelled`, `in_production` or `dormant`. |
| **Pins belong to Plex users** (Marc, 2026-11-05) | `pins.user_id` is `users.id`. The admin pins as their own Plex user (`users.is_owner`). Viewers pin from `/me`. |
| **Arrival = in Plex, by polling** (Marc, 2026-11-05) | No Sonarr webhook. "Arrived" means the recently-added check has seen the episode in Plex, which is a stronger claim than Sonarr's `has_file`. The delay is up to `library.recent_check_min` (10; can be set to 2–3). |

Not ported, because Binge already covers it: Tautulli sync, Pinnarr's own users, auth, API keys, backups, health, poster cache, the `plex_availability` job, and the films sync.

Parked for later (don't build, don't block): the MCP server, cast cross-referencing, TMDB "more like this" suggestions, TMDB episode ratings, and an iCal feed.

## Identity

A show is identified by its **TVDB id**: `media_items.tvdb_id` on the Plex show row, and `sonarr_series.tvdb_id`. This is the same match the collection pages already use.

You can only pin a show that is in Plex or in Sonarr. A Sonarr-only show (nothing on disk yet) can be pinned and shows on the calendar. It has no item page, so its card links to Sonarr.

## Schema (migration v10)

```sql
CREATE TABLE pins (
  user_id   INTEGER NOT NULL REFERENCES users(id),
  tvdb_id   INTEGER NOT NULL,
  pinned_at INTEGER NOT NULL,
  batch     TEXT,                      -- set by a bulk pin, for undo
  notify    INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, tvdb_id)
);
CREATE INDEX pins_tvdb ON pins(tvdb_id);

-- Every Sonarr episode for shows anyone has pinned (full guide), plus the calendar window for all shows.
CREATE TABLE sonarr_episodes (
  series_id INTEGER NOT NULL, season INTEGER NOT NULL, episode INTEGER NOT NULL,
  title TEXT, air_at INTEGER, has_file INTEGER, monitored INTEGER,
  PRIMARY KEY (series_id, season, episode)
);
CREATE INDEX sonarr_episodes_air ON sonarr_episodes(air_at);

-- TMDB production status, one row per show with a TMDB id.
CREATE TABLE tmdb_shows (
  tmdb_id INTEGER PRIMARY KEY, tvdb_id INTEGER, status TEXT,   -- Returning Series | In Production | Ended | Canceled | ...
  in_production INTEGER, last_air_date TEXT, next_season INTEGER, fetched_at INTEGER
);
CREATE INDEX tmdb_shows_tvdb ON tmdb_shows(tvdb_id);

-- Per-person dedupe for pin notifications.
CREATE TABLE pin_notified (
  user_id INTEGER NOT NULL, key TEXT NOT NULL, at INTEGER NOT NULL,   -- key: ep:<tvdb>:<s>:<e> | move:<tvdb>:<s>:<e>:<air_at> | ...
  PRIMARY KEY (user_id, key)
);

ALTER TABLE sonarr_series ADD COLUMN network TEXT;
ALTER TABLE sonarr_series ADD COLUMN genres TEXT NOT NULL DEFAULT '[]';
ALTER TABLE sonarr_series ADD COLUMN tmdb_id INTEGER;
```

`sonarr_upcoming` stays as it is: the dashboard's "Airing this week" reads it. Once the calendar is built, it can read `sonarr_episodes` instead, and `sonarr_upcoming` can be dropped in a later migration.

Add a test that upgrades a v9 database.

## Sync

All read-only. These go in the existing `arr_sync` job unless stated otherwise.

- **Sonarr calendar:** read −7 to +60 days for all series (one request) into `sonarr_episodes`. The current 8-day read stays for `sonarr_upcoming`.
- **Full guides:** `episode?seriesId=` for each pinned show, on every Sonarr sync and when a show is first pinned (a `pin_guides` job). One request per pinned show, not per library show. A show that can't be read is logged and skipped; it doesn't fail the sync.
- **Series:** also store `network`, `genres` and `tmdbId` (Sonarr v4 returns `tmdbId` on series).
- **TMDB** (built 2026-11-05, P2 step 1): the `tmdb_sync` job reads `tv/{id}` for every show with a TMDB id (from Plex or Sonarr). Changed from the plan of 200 a night (Marc, 2026-11-05):
  - **The first run reads everything** (about 1,900 shows, a few minutes at ~20 requests a second). Rows are written every 50, so a run that stops keeps what it read.
  - **After that** the job runs every 6 hours and reads what's due: pinned shows after 20 hours (so about daily), everything else after 7 days. Pinned shows go first.
  - **Stored** in `tmdb_shows` (migration v11 adds `name`, `next_air_date`, `last_season`, `max_season`, `genres`, `networks`). A 404 keeps a row with no status, so the id isn't asked for again until the weekly refresh.
  - **The key** is in Settings → TMDB (masked, `tmdb.api_key`). TMDB's API key (sent as `api_key=`) or its read access token (sent as a bearer token) both work. Saving a key starts a read straight away.
  - **The client** is in `bingestats/tmdb/` and has GET only. It waits and retries on 429. A failure is kept in `arr_status` as `app = 'tmdb'` and shown in Settings.
- **Sonarr queue:** already read by the downloads watcher. The calendar uses its match (`downloadId` → episode) for the "downloading" state and its progress.

## Episode state

A pure function in `services/pins.py`, worked out when the page renders and never stored. First match wins:

| State | Label | Condition |
|---|---|---|
| `available` | In Plex | In `media_items` (episode of the show with this TVDB id, season and episode, `is_present = 1`) |
| `on_disk` | Downloaded | Sonarr has the file, Plex doesn't have the episode yet |
| `unmonitored` | Not wanted | Sonarr's `monitored = 0` |
| `downloading` | Downloading n% | In the live download queue (SABnzbd job matched to the episode through Sonarr's queue) |
| `upcoming` | Upcoming | No air date |
| `airing_today` | Today | Airs, or aired, today in `ui.timezone` |
| `upcoming` | Upcoming | `air_at` > now |
| `awaiting` | Expected | Aired < 48 h ago |
| `missing` | Missing | Aired ≥ 48 h ago |

Specials (season 0) and unmonitored episodes are hidden by default: settings `pins.show_specials` and `pins.show_unmonitored`, both false. An unmonitored episode never counts as missing. Port Pinnarr's tests for this ordering (`tests/test_calendar.py` there).

## Season outlook

Port Pinnarr's engine (SPEC.md section 10 there) and its tests as they are, including `test_ended_outranks_hiatus` and `test_dated_beats_a_stale_ended_status`. First match wins:

1. `dated`: next airing is in the future.
2. `announced`: a later season exists with no air dates.
3. `in_production`: TMDB says so, and nothing is scheduled.
4. `cancelled`: TMDB `Canceled`.
5. `ended`: TMDB `Ended`, or Sonarr `ended`.
6. `between_seasons`: last aired within 9 months.
7. `dormant`: last aired more than 24 months ago and not in production (18 in the first plan; Marc moved it to 24 on 2026-11-05).
8. `unknown`.

**As built (2026-11-05, P2 step 2)**, in `services/outlook.py`, following Pinnarr's code where it differed from the list above:
- **Hiatus vs dormant uses only the last air date:** dormant is more than 24 months, hiatus anything less. It works without TMDB. Without TMDB, only filming and cancelled can't be told.
- **Planned:** a show that has never aired, with TMDB status Planned/Pilot or Sonarr status upcoming, is announced.
- **Inputs:**
  - the next airing is Sonarr's, else TMDB's next episode date;
  - the last airing is the later of Sonarr's and TMDB's;
  - the latest season is the highest that Sonarr or TMDB lists.
- **The latest aired season** comes from TMDB's last episode. Without TMDB it comes only from episode lists we hold in full (pinned shows), so an unmonitored later season in Sonarr can't pass for an announced one.
- **Worked out on query**, no cache: it's one pass over about 2,000 rows.
- **Where it shows:** a badge on show pages. On the calendar, "No future dates planned" is grouped by outlook, with "Probably over" folded away. A date only TMDB knows moves a show into "Later".

## Pages

All admin pages also work as viewer pages under `me`, scoped to `session["viewer"]`. The admin's pins are the owner's.

- **`/calendar`** (sidebar, under Dashboard): the next 14 days as an agenda (poster, show, episode, state badge, download % when downloading). Then "Aired, not arrived" (`awaiting` and `missing`). Then a month grid that names the shows on each day, linking to the item page. Then "Announced", "In production" and "Dormant" (collapsed). Empty state: "Pin some shows" with a link to `/shows`.
  - **Month view, as built (2026-11-05, P2 done):** a "Month" section after "Coming up" (`partials/calendar_month.html`, `pins.month`).
    - **Desktop:** a Monday-first grid. Each day names up to three shows, with a dot coloured by state, then "+N more". Each name links to the episode, else the show (admin only).
    - **Phones:** one compact line per episode (day, dot, show, SxxEyy), since seven columns are too narrow for names.
    - **Moving between months:** ‹ › and "This month" swap only the section (`/calendar/month?m=YYYY-MM`, `/me/calendar/month`).
    - **Shared logic:** the month view uses the same episode logic as the agenda (`pins._episodes`), so they can't disagree.
- **`/shows`**: the shows browser. Poster grid, 60 per page.
  - **Facets:** library, outlook, Sonarr status, genre, network, pinned. AND across facets, OR within one. Each facet's counts are worked out with that facet left out.
  - **Sort:** recently watched (by you), title, next airing, outlook.
  - **URL:** filters live in the query string.
  - **Pin all shown:** posts the query string, not ids, re-runs the filter on the server, and asks first with the count. "Undo last bulk pin" uses `pins.batch`.
  - **Phones:** the facet rail becomes a sheet.
  - **As built (2026-11-05, P2 step 3)** in `services/shows.py`, `templates/pages/shows.html`:
    - **Which shows:** every show with a TVDB id, from Plex or Sonarr (Sonarr-only shows count as library "Not in Plex").
    - **Filters:** genre is Sonarr's genres plus TMDB's. Network is Sonarr's, else TMDB's first.
    - **"Recently watched"** is your own plays, matched to the show by guid or rating key.
    - **"Pin all"** only appears once something is filtered, and asks first with the count. "Undo last bulk pin" removes the newest batch.
    - **Updating:** changing a filter swaps the page in place and keeps the URL short (empty fields left out). The search box keeps focus. A pin reloads the counts.
    - **Pin icons:** on posters, a pinned show's icon is a filled circle.
    - **In the nav** next to Calendar, while Sonarr is set up, and linked from the calendar's search box.
- **Item page (show):** a pin toggle; outlook badge; the next episode and its state.
- **`/me/calendar`, `/me/shows`:** the same templates, on the viewer's own pins.
- **Lists** (tabs on `/calendar`):
  - **Ready to watch:** pinned shows with `available` episodes you haven't played (by guid, from `plays`), oldest unwatched first.
  - **Retire:** pins that are `ended` or `cancelled` and fully watched. "Unpin these" re-runs the query; undo uses `batch`.
  - **Discover:** unpinned shows you've watched before that have just got a date (`dated` with `next_airing` within 60 days). Admin and viewer.
- **Downloads page:** a pin badge on jobs for shows the admin has pinned, and a "stalled" flag (no progress for 30 min).

## Notifications

New triggers in `services/notify.py`, group `pins`:

| Trigger | When | Context |
|---|---|---|
| `pin_arrived` | The recently-added digest groups a show/season that someone has pinned | `pinner` (user), `show`, `season`, `count`, `items`, `headline` |
| `pin_schedule_changed` | A pinned episode's `air_at` moves by more than 1 h, from the nightly sync | `pinner`, `show`, `episode`, `old_at`, `new_at` |
| `pins_week` | Monday 08:00 in `ui.timezone` | `pinner`, `days` (episodes by day) |

- **One event per pinner** whose pin has `notify = 1`. Condition field `pinner.id`, so the admin writes one rule per person with that person's channel. The rules engine is unchanged.
- **Dedupe** goes through `pin_notified`, so an upgrade (same episode again) never fires twice and one person's message never suppresses another's.
- **Default rules:** add "Pinned show arrived" and "Pinned week ahead", disabled and with no channel, like the existing defaults.

## Home Assistant

Per tracked user:
- `{name} next pinned`: sensor; the state is "Show S01E02"; attributes: `air_at`, `state`.
- `{name} pinned ready`: sensor; count of ready-to-watch episodes.

Both are published with the rest of state.

## REST API

All read scope, except pin and unpin (write scope). They act for `user_id` (default: the owner).

| Method | Path |
|---|---|
| GET | `/api/v1/pins?user_id` |
| PUT / DELETE | `/api/v1/pins/{tvdb_id}?user_id` |
| GET | `/api/v1/pins/summary?user_id`: next, today, aired not arrived, ready count, failing syncs. One call for a wall display |
| GET | `/api/v1/pins/schedule?user_id&days=7&back=1` |
| GET | `/api/v1/pins/arrivals?user_id&hours=24` |
| GET | `/api/v1/pins/widget?user_id`: pre-worded lines for the Scriptable widget |

Document these in `docs/API.md`. Port Pinnarr's Scriptable widget into `docs/widget/` and point it at `/api/v1/pins/widget`.

## Importing from Pinnarr

A job, `import_pinnarr`, run from Settings → Import, with a dry run first like the Tautulli importer. It reads an uploaded copy of `pinnarr.db`:

- **Accounts:** each Pinnarr account maps to a Binge user by `users.plex_username` (case-insensitive). Accounts without one, or with no match, are listed in the dry run and skipped. An admin with no `plex_username` can be mapped to the owner by ticking a box.
- **Pins:** `pins` → `series.tvdb_id`, keeping `pinned_at` and `notify`.
- **Dedupe:** `episode_notifications` → `pin_notified`, so nothing that was already announced gets announced again.
- **Repeats:** running the import twice changes nothing.

Then Pinnarr can be stopped.

## Milestones

| # | Milestone | Done when |
|---|---|---|
| P1 | Migration v10, calendar and guide sync, episode state, `/calendar` agenda + "aired, not arrived", pin toggle on item pages, `/me` versions | Marc's pins (by hand) show the right states against the live server |
| P2 | TMDB client and job, outlook, `/shows` browser with facets and bulk pin, month grid, announced / in production / dormant | The outlook of 20 shows checked by hand matches reality, or is wrong only where TMDB is |
| P3 | Triggers, dedupe, default rules, HA sensors | An episode landing in Plex sends one message per pinner; an upgrade sends none |
| P4 | Ready, retire, discover, downloads badge | The lists make sense against the real library |
| P5 | API, widget, Pinnarr importer | Pinnarr's pins and notified log imported; Pinnarr stopped |

Every milestone adds tests, as CLAUDE.md requires. Drive the sync with fixtures from Sonarr and TMDB in `tests/fixtures/`.

## Verify against live

Write findings in `docs/verification.md`:
- Sonarr v4 `series` returns `tmdbId`, `network` and `genres`. What share of Marc's shows have a `tmdbId`?
- How long `calendar` takes for −7 to +60 days across the library.
- Whether a Sonarr upgrade gives the episode a new Plex rating key (if it does, the recently-added check sees it as new; `pin_notified` must still stop the repeat).
