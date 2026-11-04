# Verification against live

Findings from checking the spec against Marc's real Plex server and Tautulli database. One section per "Verify against live" item in SPEC.md.

## Plex `/status/sessions` shape (SPEC 4)

Checked 2026-10-31 against PMS **1.43.5.11079** (LXC 601), with a live direct-play episode session from Plex for Windows on the LAN.

- `Accept: application/json` works. Sessions are in `MediaContainer.Metadata[]`.
- `Session.id` is a string such as `ugzpro0u9qbnnc0he8th2aw7`, which is what we key on. `Session` also carries `bandwidth` (kbps) and `location` (`lan`/`wan`).
- `ratingKey`, `parentRatingKey`, `grandparentRatingKey`, `librarySectionID` and `User.id` are **strings**, so parse with `int()`. `Player.userID` is an int with the same account id.
- `guid` is `plex://episode/…`, `grandparentGuid` is `plex://show/…` and `parentGuid` is `plex://season/…`. All three are present on sessions, so `grandparent_guid` comes for free on live plays.
- `duration` and `viewOffset` are in **milliseconds**.
- `Player`: `address` (the LAN IP when local), `remotePublicAddress`, `local`, `relayed`, `secure`, `state`, `title`, `product`, `platform`, `machineIdentifier`, and `model` (there is no `device` field from Plex for Windows).
- A direct play has **no `TranscodeSession`**, and `Part.decision` is `directplay`. The video stream has `colorTrc` (`bt709`), which is what dynamic range is read from.
- `tests/fixtures/plex/sessions_direct_play.json` is this response with names, IPs and machine ids replaced and the cast list dropped.

**Direct stream, seen live 2026-10-31** (episode, MKV source, DASH client):
- `TranscodeSession` is present with `videoDecision` and `audioDecision` both `copy`, `container: mp4` and `protocol: dash`.
- `Part.decision` is `transcode` even though nothing is re-encoded, and each `Stream` has `decision: copy`. Don't use `Part.decision` to tell a transcode apart.
- Session `Media` describes the **output**: `container: mp4` while the file is MKV, and `videoResolution: "1080p"` with a trailing `p`, which the parser strips.
- `throttled: true` with `speed: 0.0` once the buffer is full, so a low speed means nothing while throttled. The `transcoder_slow` rule already ignores throttled sessions.
- `transcodeHwRequested: true` and `transcodeHwFullPipeline: false`, with no `transcodeHwDecoding` / `transcodeHwEncoding` keys at all when only copying. HW correctly reads as false.
- The poller looked up the source once (MKV, H.264 1080p, AAC 2.0) and recorded `direct_stream` with reason `container`, as intended.

**Not yet seen live:** a video transcode, a subtitle burn or music. `sessions_transcode.json` and `metadata_5001.json` are synthetic, built from the PMS shape as Tautulli documents it. In a transcode, `Media`/`Part`/`Stream` describe the **output**, so the poller fetches `/library/metadata/{ratingKey}` once per session to get the source file (codec, resolution, HDR, channels). Next time something transcodes, capture it with:

```bash
curl -s -H 'Accept: application/json' "http://127.0.0.1:32400/status/sessions?X-Plex-Token=$TOKEN"
```

Then compare it with the fixture.

## plex.tv users endpoints (SPEC 4)

Checked 2026-10-31:
- `GET https://plex.tv/api/v2/friends` returns **410 Gone**. Don't use it.
- `GET https://plex.tv/api/users` is XML only. It lists **shared** users (3 here) with `id`, `title`, `username`, `email`, `thumb` and `home`.
- `GET https://plex.tv/api/v2/home/users` returns JSON `{users: [...]}` for Plex Home (7 here). Each has `id`, `title`, `username`, `friendlyName`, `email`, `thumb`, `admin`, `guest` and `restricted`. The owner is in this list with `admin: true`.
- `GET https://plex.tv/api/v2/user` returns the owner. Its `id` (5342218 here) is the same account id Tautulli stores.
- **The owner is user `1` in `/status/sessions`**, not their account id. The poller maps 1 to the owner once user sync has found them, and user sync merges anything already recorded under 1 into the owner.

## Hardware transcode fields (SPEC 6)

Tautulli's media info table stores `transcode_hw_decoding`, `transcode_hw_encoding`, `transcode_hw_full_pipeline` and `transcode_hw_requested`, which matches the `TranscodeSession` attributes `transcodeHwDecoding`, `transcodeHwEncoding`, `transcodeHwFullPipeline` and `transcodeHwRequested`. The parser treats a truthy value in any of the first three as HW. Still to confirm from a live transcode.

## Tautulli schema and units (SPEC 8)

Checked 2026-10-31 against Tautulli **v2.18.2**, `/opt/Tautulli/tautulli.db` on LXC 408, read-only.

- **5,545 plays** from 2024-09-29 to today: 5,051 episodes, 468 movies, 26 tracks. **14 users**, 10 library sections.
- `session_history.view_offset` and `session_history_metadata.duration` / `session_history_media_info.duration` are in **milliseconds**. `started`, `stopped` and `paused_counter` are in seconds.
- `reference_id` groups resumes: 5,545 rows into 4,454 groups.
- `session_history_metadata.guid`: 4,466 `plex://episode`, 458 `plex://movie`, 13 `plex://track`. There are also **235 legacy `com.plexapp.agents.*` guids** and a few `local://` ones. Matching by guid works for most rows; the rest fall back to `rating_key`.
- **There is no `grandparent_guid` column.** Episodes have to get their show guid from `media_items` (library sync) by `grandparent_rating_key`, as the spec planned.
- `transcode_decision` values are `direct play` (4,715), `copy` (148) and `transcode` (682). `video_decision` / `audio_decision` use the same three words. `stream_subtitle_decision` is `''`, `burn`, `copy`, `ignore` or `transcode`.
- `users` has its own `id` plus `user_id`, which is the Plex account id, so map `user_id → users.id`. There is a `user_id = 0` row (Tautulli's "Local" user). Every play's `user_id` exists in `users`.
- `location` is `lan`, `wan` or `cellular`.
- Target-side columns are `stream_*` (for example `stream_video_resolution`, `stream_audio_channels`). Source-side columns are unprefixed (`video_resolution`, `audio_channels`). `transcode_hw_decoding` and `transcode_hw_encoding` are integers.
- `tests/fixtures/tautulli_schema.sql` is this database's `.schema` for the four tables the importer reads.

**Import run, 2026-10-31.** The library sync took 37 s for 56,800 items. The import then took 4.4 s:
- 5,547 plays imported and 0 failed. Per user: Sarah 2,894, mltew 2,390, Wazzaboothy 260, natalielangbridge 3, which matches Tautulli's counts exactly.
- 1,091 resumed plays linked to their first play, matching Tautulli's grouping (rows minus groups).
- 3,822 plays had a show guid or a modern `plex://` guid filled in from the library.
- 1,375 episode plays still have no show guid. Those shows are no longer in Plex (Desperate Housewives, Pretty Little Liars, Suits and others), or came from a library (section 2) that no longer exists. They group by show title instead.
- 235 plays keep legacy `com.plexapp.agents.*` guids and about 230 have `local://` guids (Sport and YouTube libraries with no agent). Those match by rating key only.

## Stats and geo on real data (M3), 2026-10-31

- **Stats against hand SQL:**
  - all-time plays: 4,456 group heads, matching `SUM(group_id IS NULL)`;
  - watch time: 11,037,591 s, matching `SUM(play_s)`;
  - the 30-day daily series sums to the 30-day summary (266);
  - the heatmap's hours sum to total watch time (3,066 h).
- **DB-IP Lite City:**
  - The `.mmdb.gz` is about 60 MB and unpacks to about 127 MB. It is published monthly at `download.db-ip.com/free/dbip-city-lite-YYYY-MM.mmdb.gz`.
  - The first download hit a one-off TLS "record layer failure" and fell back to the previous month's file. Downloads now retry once per month before falling back.
- **Locations:**
  - 90 distinct IPs in history. 61 are private: LAN, plus Tailscale's 100.64/10 range, which shows as "Private network or VPN". The other 29 all resolved.
  - Most plays come from the home network (3,757). After that come VPN (431) and a handful of UK towns.
- **DNS on LXC 406 is intermittent.** `cdn.jsdelivr.net` and GitHub release hosts failed to resolve a few times today, with 192.168.4.1 as the resolver. Builds survive it because `fetch-assets.sh` retries five times, but the resolver is worth a look.

## Limits and Home Assistant (M5), 2026-10-31

- **Migration v3 on the live database:** ran cleanly. Schema is now 3, and the two limit rules were added, disabled.
- **Broker:** Mosquitto on the `mqtt` LXC (192.168.4.121:1883), `allow_anonymous false`. A `bingestats` user was added with `mosquitto_passwd` and the broker reloaded. A backup of the old password file sits next to it as `passwd.bak-*`.
  - Mosquitto warns that `/etc/mosquitto/passwd` is owned by `mosquitto`, not root, and that future versions will refuse to load it. This was the case before Binge Stats.
- **Discovery:** HA created one "Binge Stats" device with 13 entities: 11 server-wide plus the tracked user's sensor and button. Entity ids are `sensor.binge_stats_*`, `binary_sensor.binge_stats_plex_server` and `button.binge_stats_stop_<name>_s_streams`.
- **Live values matched Binge Stats:**
  - 2 active streams, both direct play;
  - 14.8 Mbit/s total, made up of 4.1 LAN and 10.7 WAN;
  - Plex up;
  - 8 plays and 5.01 h today;
  - 2 unacknowledged alerts.
- **Attributes:** the Active streams attributes list each session (user, title, player, progress).
- **Stop button:** pressed in HA for an idle user. The worker logged the command within a second ("stopped 0 stream(s)"), so the command path works end to end. It wasn't tested against someone else's real stream.
- **Not yet seen live:** a stream limit or sharing alert firing on real traffic. Both are covered by tests with real session fixtures.

## Insights on real data (M6), 2026-10-31

- **Show matching:**
  - 1,375 imported episode plays had no show guid. They came from shows that had been re-added to Plex (new rating keys), and most also from before Plex's agent change (legacy `com.plexapp.agents.thetvdb://` episode guids).
  - The backfill recovered them in three steps: through the episode guid, then by unique show title, then the episode guid by season and episode number. 29 are left, all from shows no longer in the library.
  - Without this, Desperate Housewives (now 187 plays, 126 h) and Pretty Little Liars read as never watched.
- **Binges:** 587 across three people.
  - Sarah leads, with a longest of 39 episodes of The Bill on 4 June (29 h, which looks like autoplay overnight).
  - Most binged shows: Desperate Housewives, Pretty Little Liars, Gilmore Girls, Gossip Girl.
  - The rebuild takes under a second.
- **Library health:** 122.8 TB across 5,560 films and shows.
  - Stale (nothing played in 12 months, added before that): 2,917 items, 38.4 TB.
  - Never watched (added 90+ days ago): 4,656 items, 84.0 TB.
  - The biggest stale items are Life Below Zero (740 GB), Gilmore Girls (698 GB, last watched by Sarah) and House (570 GB).
  - The worst GB per hour watched is On Patrol: Live, at 1.97 TB never watched.
  - The page computes in 0.2 s.
- **Transcodes, last 90 days:** 60 of 535 plays (11 %).
  - 93 % of them are "because of the file", but nearly all of those are audio: EAC3, AC3 and DTS.
  - Plex Web transcodes 79 % of its plays; Apple TV transcodes 4 %.
  - The five re-encode candidates are all EAC3/AC3 episodes. The real fix is the client, since browsers can't play EAC3, rather than re-encoding the files.
- **Completion:** the heaviest viewer has 20 shows finished and several abandoned, e.g. What We Do in the Shadows (17 of 61) and The Bill (200 of 289).

## Wrapped (M7), 2026-10-31

- **Daily job:** the `wrapped` job ran on deploy and cached 2026 for the server and four people. Each computes in 0.03–0.21 s, so a cache miss is fine to serve live.
- **Server-wide 2026:** 1,217.6 h ("55 trips London → Sydney").
  - Top shows: The Bill, Made in Chelsea, Gossip Girl.
  - Longest binge: The Bill, 39 episodes.
  - Busiest day: 17 October, 15.8 h.
  - 22 shows finished.
- **Per person:**
  - Sarah: #1 of 4, 689.7 h, 6 shows finished.
  - Marc: #2, 474.5 h, top show Police Interceptors, longest binge Letterkenny ×10, 16 shows finished.
- **Share link, tested from the LAN with no login:**
  - The page is 200, 59 KB, 0.06 s, with no CSRF token in it.
  - Its own poster through the scoped proxy is 200 (`image/jpeg`, 37 KB). Any other artwork path is 404.
  - The admin page without login redirects to the login page (302).
  - After revoke, both the page and its image return 404.
- **Not checked from here:**
  - opening a share link on a real phone, from outside the LAN;
  - whether the server's real posters look right in the hero collage.

## Polish (M8), 2026-10-31

- **Backups:** after the deploy, the worker ran the owed nightly backup by itself within a second of starting.
  - The file is 25.6 MB and self-contained.
  - It matches the live database (5,554 plays, 15 users, 56,804 library items, schema v6), and `PRAGMA integrity_check` returns `ok`.
- **CI:** the first run on GitHub Actions passed pyflakes and all 287 tests in 25 s.
- **Fresh install** (checked locally against an empty database):
  - every page has an empty state, and none errors;
  - the dashboard and the new "Get started" step both point to the Tautulli import;
  - library health and Wrapped explain themselves when there's nothing yet.
- **Light theme:** every page checked against the same data; there are no dark-only overlays.

## Viewer logins, 2026-10-31

Tested on the live site through `https://binge.marctew.co.uk` with a temporary login for the owner's own Plex account (`mltew`), removed straight after.

- **Sign-in:** the uppercase username `MLTEW` signed in and landed on `/me` ("Hi Marc Tew"). Nobody else's name appears on the page.
- **Admin pages:** `/`, `/history`, `/users`, another person's user page, `/settings`, `/insights/library-health` and another person's Wrapped all redirect to `/me`.
- **History:** `/me/history?user=<someone else>` still shows only the viewer's own plays.
- **API and actions:** `/api/v1/history` returns 401. POST `/users/<someone else>/stop` returns 403.
- **Removal:** after the login was removed, the same session's next request went to the login page.

## Collection and Playback, 2026-10-31

- **Data available:** codec, resolution, audio codec, container and size are filled for all 44,158 films and episodes in the library. HDR (`video_dynamic_range`) is never filled, because Plex's library listing doesn't carry it, so the page doesn't show HDR.
- **Make-up:** 122.8 TB in total, of which 109.0 TB is in older codecs (H.264, MPEG-2, VC-1). The estimated saving from re-encoding is 54.6 TB. 836 files are below 1080p.
  - The biggest re-encode wins are On Patrol: Live (366 files, about 973 GB), NCIS (about 633 GB) and Made in Chelsea (about 428 GB).
  - Each list computes in 0.05–0.3 s.
- **Upgrade candidates:** 163 rows, led by seasons of The Bill in 720p/576p/SD, which are also the most played.
- **Gaps:** 270 shows with 1,528 missing episodes and 177 missing seasons.
  - Sorted by raw count, the top was shows Marc dips into: Beat Bobby Flay, Buzzcocks, The Daily Show. Sorting by how complete the show is fixed that.
  - The top is now one-episode holes: Scrubs S08 E18, Ghosts (US) S01 E06, Numb3rs S03 E02, Property Brothers S10 E01.
- **Playback (last 30 days):** 8 live plays, 4 of which buffered (10 stalls), so 20.7 per 10 hours. That's a small sample; Tautulli's imported history has no buffering data.

## Sonarr, Radarr and SABnzbd, 2026-10-31

API shapes were checked against the live apps before building: Sonarr 4.0.19, Radarr 6.3.0, SABnzbd 5.1.3. Connected with each app's own API key.

- **First sync, no errors:**
  - Sonarr: 2,142 series, 13,014 monitored aired episodes missing, 94 below cutoff, 109 airing in the next 8 days.
  - Radarr: 3,833 films, 472 monitored and released but missing, 371 collections.
  - SABnzbd: 439 days of daily totals (80.7 TB in all, since 7 Aug 2025). Its history is empty because it gets cleared, and its queue was idle.
- **Plex external ids** after a library sync: 1,822 of 1,872 shows have a TVDB id, and 3,536 of 3,689 films have a TMDB id.
- **Matching:**
  - 1,808 of the 1,870 Sonarr series that have files match a Plex show by TVDB id (97 %).
  - 3,256 of the 3,359 Radarr films that have files match a Plex film by TMDB id (97 %).
  - The rest are mostly items whose Plex agent has no TVDB/TMDB id.
- **Shows:** 1,009 complete and ended, 483 up to date and still airing, 623 behind.
  - Gaps from Sonarr, nearly-complete first: Great British Railway Journeys S16 E15, Property Brothers S10 E01, Numb3rs S03 E02.
- **Films:**
  - 186 of 371 collections are complete. Nearly there: Hannah Swensen Mysteries 14/15, Beethoven 7/8, Jurassic Park 7/8, Scream 7/8.
  - 45 files are below the quality cutoff.
- **Downloads:** 3.89 TB in the last 30 days. Biggest day was 27 Sep 2026, at 2.78 TB.
- **Speed:** each query computes in under 0.1 s. The sync takes about 20 s.

## Live downloads, 2026-11-04

- **Matching:** checked against Sonarr 4 and Radarr 6.
  - Their `/api/v3/queue` records carry `downloadId`, which is SABnzbd's `nzo_id`, so a SAB job matches exactly; no name parsing.
  - Their download-client settings name the SABnzbd categories they use: Sonarr `tv`, Radarr `movies`.
- **Posters:** `/api/v3/mediacover/<id>/poster-250.jpg` answers with a JPEG when sent the API key header, so posters work for downloads that aren't in Plex yet.
- **Live, after deploying:**
  - The watcher reads SABnzbd's queue every 3 s while it's busy: 200 jobs in detail, 1,401 in the header, about 108 MB/s.
  - Nothing in the queue was from Sonarr or Radarr. Everything was in another category, so the page showed "Nothing downloading" with 1,401 jobs counted and hidden. No names appeared on Downloads or Download stats.
- **Not seen live yet:** a Sonarr/Radarr grab moving through the page. That flow is covered by tests that use Sonarr and Radarr queue records shaped like the real ones.
