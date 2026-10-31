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
