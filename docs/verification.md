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

**Not yet seen live:** a transcode, a direct stream, a subtitle burn or music. `sessions_transcode.json` and `metadata_5001.json` are synthetic, built from the PMS shape as Tautulli documents it. In a transcode, `Media`/`Part`/`Stream` describe the **output**, so the poller fetches `/library/metadata/{ratingKey}` once per session to get the source file (codec, resolution, HDR, channels). Next time something transcodes, capture it with:

```bash
curl -s -H 'Accept: application/json' "http://127.0.0.1:32400/status/sessions?X-Plex-Token=$TOKEN"
```

Then compare it with the fixture.

## plex.tv users endpoints (SPEC 4)

_Not yet checked; needed for M2 user sync._

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
- `tests/fixtures/tautulli_schema.sql` will be cut from this database's `.schema` in M2.
