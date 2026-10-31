"""Session state machine: /status/sessions -> active_sessions -> plays.

SPEC.md section 5. The poller owns `active_sessions`, `plays`, `servers` and
creates stub `users`. It takes `now` from an injected clock so tests can drive
it with fixture responses and frozen time.

Network calls (sessions, source lookups) happen before the write transaction,
so the database lock is never held while waiting on Plex.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pathlib import Path

from .. import db
from . import guard
from ..plex.client import PlexClient, PlexError
from ..plex.models import PlexSession
from ..plex.parse import build_stream, parse_identity, parse_sessions, parse_source_media
from ..services import binges, settings

log = logging.getLogger(__name__)

EventSink = Callable[[str, dict[str, Any]], None]

SERVER_DOWN_AFTER = 3          # consecutive failed polls
BUFFER_WINDOW_S = 300
BUFFER_EVENT_COUNT = 3
SRC_LOOKUP_ATTEMPTS = 3
GROUP_OFFSET_TOLERANCE_S = 120
# In /status/sessions Plex reports the server owner as user "1", not their
# plex.tv account id. Map it to the real account once user sync knows it.
LOCAL_OWNER_ID = 1


def owner_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM users WHERE is_owner = 1 AND id != ? ORDER BY id LIMIT 1",
                       (LOCAL_OWNER_ID,)).fetchone()
    return row["id"] if row else None


@dataclass
class PollResult:
    ok: bool
    sessions: int = 0
    started: list[str] = field(default_factory=list)
    finalised: list[int | None] = field(default_factory=list)   # play ids (None = dropped)
    events: list[tuple[str, dict]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)


class Poller:
    def __init__(self, conn: sqlite3.Connection, client: PlexClient,
                 clock: Callable[[], float] = time.time, on_event: EventSink | None = None,
                 data_dir: Path | None = None):
        self.conn = conn
        self.client = client
        # where the geo database lives; defaults to the database's own folder
        self.data_dir = Path(data_dir) if data_dir else Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
        self._stops: list[tuple[str, str]] = []
        self._live: set[str] = set()
        self.clock = clock
        self.on_event = on_event or (lambda t, c: None)
        self.failures = 0
        self.server_id: int | None = None
        self._load_server()

    # -- server -----------------------------------------------------------------

    def _load_server(self) -> None:
        row = self.conn.execute("SELECT id FROM servers ORDER BY id LIMIT 1").fetchone()
        self.server_id = row["id"] if row else None

    def ensure_server(self) -> int:
        """Look up the server's identity and upsert it. Raises PlexError."""
        ident = parse_identity(self.client.identity(), self.client.root())
        now = int(self.clock())
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO servers (machine_id, name, version, last_seen_at, is_up) VALUES (?,?,?,?,1) "
                "ON CONFLICT(machine_id) DO UPDATE SET name=excluded.name, version=excluded.version, "
                "last_seen_at=excluded.last_seen_at",
                (ident.machine_id, ident.name, ident.version, now),
            )
        row = self.conn.execute("SELECT id FROM servers WHERE machine_id = ?", (ident.machine_id,)).fetchone()
        self.server_id = row["id"]
        return self.server_id

    def _server_is_up(self) -> bool:
        row = self.conn.execute("SELECT is_up FROM servers WHERE id = ?", (self.server_id,)).fetchone()
        return bool(row and row["is_up"])

    def _server_row(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM servers WHERE id = ?", (self.server_id,)).fetchone()

    # -- poll -------------------------------------------------------------------

    def poll_once(self) -> PollResult:
        now = int(self.clock())
        try:
            if self.server_id is None:
                self.ensure_server()
            data = self.client.sessions()
        except PlexError as e:
            return self._poll_failed(now, e)

        result = PollResult(ok=True)
        events = result.events
        if not self._server_is_up():
            srv = self._server_row()
            down_since = srv["last_seen_at"] if srv and srv["last_seen_at"] else now
            with db.transaction(self.conn):
                self.conn.execute("UPDATE servers SET is_up = 1 WHERE id = ?", (self.server_id,))
                self.conn.execute(
                    "INSERT INTO alerts (created_at, kind, detail) VALUES (?, 'server_up', ?)",
                    (now, json.dumps({"down_for_s": now - down_since})),
                )
            if srv is not None and not srv["is_up"]:
                events.append(("server_up", {"server": dict(srv), "down_for_s": now - down_since}))
        self.failures = 0

        cfg = settings.get_many(self.conn, "ingest.")
        parsed = parse_sessions(data)
        owner = owner_id(self.conn)
        for s, _raw in parsed:
            if s.user_id == LOCAL_OWNER_ID and owner is not None:
                s.user_id = owner
        result.sessions = len(parsed)
        rows = {r["session_id"]: r for r in self.conn.execute("SELECT * FROM active_sessions")}

        # Network work first: source-file lookups for transcodes.
        prepared: list[tuple[PlexSession, dict, sqlite3.Row | None, dict]] = []
        for s, raw in parsed:
            row = rows.get(s.session_id)
            snap = json.loads(row["snapshot"]) if row else {}
            if row is not None and row["rating_key"] != s.rating_key:
                snap = {}  # item changed on the same session: treat the rest as new
            prev = PlexSession.from_dict(snap["session"]) if snap.get("session") else None
            if prev is not None and prev.stream.src_from_library:
                s.stream = build_stream(raw, prev.stream.src)
            elif s.stream.has_transcode_session and s.rating_key and \
                    snap.get("src_attempts", 0) < SRC_LOOKUP_ATTEMPTS:
                snap["src_attempts"] = snap.get("src_attempts", 0) + 1
                try:
                    src = parse_source_media(self.client.metadata(s.rating_key), s.media_id)
                except PlexError as e:
                    log.debug("source lookup for %s failed: %s", s.rating_key, e)
                    src = None
                if src is not None:
                    s.stream = build_stream(raw, src)
            prepared.append((s, raw, row, snap))

        self._stops = []
        self._live = {s.session_id for s, *_ in prepared}
        with db.transaction(self.conn):
            self.conn.execute("UPDATE servers SET last_seen_at = ?, is_up = 1 WHERE id = ?",
                              (now, self.server_id))
            seen = set()
            for s, _raw, row, snap in prepared:
                seen.add(s.session_id)
                if row is not None and row["rating_key"] != s.rating_key:
                    pid = self._finalise(row, row["last_seen_at"], cfg, events)
                    result.finalised.append(pid)
                    row = None
                if row is None:
                    self._start(s, snap, now, cfg, events)
                    result.started.append(s.session_id)
                else:
                    self._update(s, row, snap, now, cfg, events)

            grace = int(cfg["ingest.grace_polls"])
            for sid, row in rows.items():
                if sid in seen:
                    continue
                missed = row["missed_polls"] + 1
                if missed >= grace:
                    result.finalised.append(self._finalise(row, row["last_seen_at"], cfg, events))
                else:
                    self.conn.execute("UPDATE active_sessions SET missed_polls = ? WHERE session_id = ?",
                                      (missed, sid))

        for session_id, message in self._stops:
            try:
                self.client.terminate(session_id, message)
                log.info("stopped session %s (limit)", session_id)
            except PlexError as e:
                log.warning("couldn't stop session %s: %s", session_id, e)
        result.stopped = [sid for sid, _m in self._stops]
        self._emit(events)
        return result

    def _poll_failed(self, now: int, err: Exception) -> PollResult:
        self.failures += 1
        log.warning("Plex poll failed (%d): %s", self.failures, err)
        result = PollResult(ok=False)
        if self.failures == SERVER_DOWN_AFTER and self.server_id is not None and self._server_is_up():
            srv = self._server_row()
            with db.transaction(self.conn):
                self.conn.execute("UPDATE servers SET is_up = 0 WHERE id = ?", (self.server_id,))
                self.conn.execute(
                    "INSERT INTO alerts (created_at, kind, detail) VALUES (?, 'server_down', ?)",
                    (now, json.dumps({"error": str(err)})),
                )
            result.events.append(("server_down", {"server": dict(srv) if srv else {}, "error": str(err)}))
            self._emit(result.events)
        # Missed polls are deliberately not counted while the server is unreachable.
        return result

    def _emit(self, events: list[tuple[str, dict]]) -> None:
        for trigger, ctx in events:
            try:
                self.on_event(trigger, ctx)
            except Exception:  # noqa: BLE001 - a broken sink must not stop ingest
                log.exception("event sink failed for %s", trigger)

    # -- transitions --------------------------------------------------------------

    def _ensure_user(self, s: PlexSession, now: int) -> None:
        self.conn.execute(
            "INSERT INTO users (id, username, friendly_name, thumb_url, share_salt, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at, "
            "username = COALESCE(users.username, excluded.username), "
            "thumb_url = COALESCE(excluded.thumb_url, users.thumb_url)",
            (s.user_id, s.user_title, None, s.user_thumb, secrets.token_hex(8), now, now),
        )

    def _start(self, s: PlexSession, snap: dict, now: int, cfg: dict, events: list) -> None:
        self._ensure_user(s, now)
        snap = {"session": s.to_dict(), "src_attempts": snap.get("src_attempts", 0),
                "fired_watched": False, "buffer_times": []}
        buffer_count = 0
        if s.state == "buffering":
            buffer_count = 1
            snap["buffer_times"] = [now]
        self.conn.execute(
            "INSERT INTO active_sessions (session_id, session_key, server_id, user_id, rating_key, media_type, "
            "started_at, last_seen_at, missed_polls, state, state_since, paused_s, buffer_count, "
            "start_offset_s, view_offset_s, duration_s, snapshot) "
            "VALUES (?,?,?,?,?,?,?,?,0,?,?,0,?,?,?,?,?)",
            (s.session_id, s.session_key, self.server_id, s.user_id, s.rating_key, s.media_type,
             now, now, s.state, now, buffer_count, s.view_offset_s, s.view_offset_s, s.duration_s,
             json.dumps(snap)),
        )
        ctx = {"session": s.to_dict()}
        events.append(("play_start", ctx))
        if s.stream.decision == "transcode":
            events.append(("transcode_start", ctx))
        self._stops += guard.on_start(self.conn, s, now, self.data_dir, events, live=self._live)

    def _update(self, s: PlexSession, row: sqlite3.Row, snap: dict, now: int, cfg: dict,
                events: list) -> None:
        prev = PlexSession.from_dict(snap["session"]) if snap.get("session") else None
        state, state_since = row["state"], row["state_since"]
        paused_s, buffer_count = row["paused_s"], row["buffer_count"]
        ctx = {"session": s.to_dict()}
        if s.state != state:
            if state == "paused":
                paused_s += max(0, now - state_since)
            if s.state == "buffering":
                buffer_count += 1
                times = [t for t in snap.get("buffer_times", []) if now - t <= BUFFER_WINDOW_S] + [now]
                if len(times) >= BUFFER_EVENT_COUNT:
                    events.append(("buffer", ctx))
                    times = []
                snap["buffer_times"] = times
            if s.state == "paused":
                events.append(("pause", ctx))
            elif state == "paused" and s.state == "playing":
                events.append(("resume", ctx))
            state, state_since = s.state, now

        if prev is not None and prev.stream.decision != "transcode" and s.stream.decision == "transcode":
            events.append(("transcode_start", ctx))
            self._stops += guard.on_transcode(self.conn, s, now, events, live=self._live)

        if not snap.get("fired_watched") and s.duration_s:
            pct = s.view_offset_s / s.duration_s * 100
            if pct >= _watched_threshold(cfg, s.media_type):
                snap["fired_watched"] = True
                events.append(("watched", ctx))

        snap["session"] = s.to_dict()
        self.conn.execute(
            "UPDATE active_sessions SET session_key=?, last_seen_at=?, missed_polls=0, state=?, state_since=?, "
            "paused_s=?, buffer_count=?, view_offset_s=?, duration_s=?, snapshot=? WHERE session_id=?",
            (s.session_key, now, state, state_since, paused_s, buffer_count, s.view_offset_s,
             s.duration_s or row["duration_s"], json.dumps(snap), s.session_id),
        )

    def _finalise(self, row: sqlite3.Row, stopped_at: int, cfg: dict, events: list) -> int | None:
        """Turn an active session into a play. Returns the play id, or None if dropped."""
        self.conn.execute("DELETE FROM active_sessions WHERE session_id = ?", (row["session_id"],))
        snap = json.loads(row["snapshot"])
        s = PlexSession.from_dict(snap["session"])

        paused = row["paused_s"]
        if row["state"] == "paused" and stopped_at > row["state_since"]:
            paused += stopped_at - row["state_since"]
        started = row["started_at"]
        play_s = max(0, stopped_at - started - paused)

        user = self.conn.execute("SELECT keep_history FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        if user is not None and not user["keep_history"]:
            return None
        if play_s < int(cfg["ingest.min_play_s"]):
            return None

        duration = row["duration_s"] or s.duration_s
        offset = row["view_offset_s"] or 0
        pct = min(100, round(offset / duration * 100)) if duration else None
        watched = int(pct is not None and pct >= _watched_threshold(cfg, s.media_type))

        group_id = self._find_group(row["user_id"], s, started, row["start_offset_s"] or 0, cfg)
        st = s.stream
        src = st.src
        cur = self.conn.execute(
            "INSERT INTO plays (server_id, group_id, source, session_id, user_id, started_at, stopped_at, "
            "paused_s, play_s, start_offset_s, view_offset_s, duration_s, percent_complete, watched, "
            "buffer_count, media_type, rating_key, parent_rating_key, grandparent_rating_key, guid, "
            "grandparent_guid, library_id, title, parent_title, grandparent_title, season_index, "
            "episode_index, year, thumb, player_title, platform, product, device, machine_id, ip_address, "
            "is_local, is_relayed, is_secure, decision, video_decision, audio_decision, subtitle_decision, "
            "transcode_reasons, hw_decode, hw_encode, src_container, src_video_codec, src_resolution, "
            "src_dynamic_range, src_audio_codec, src_audio_channels, src_bitrate_kbps, dst_container, "
            "dst_video_codec, dst_resolution, dst_audio_codec, dst_audio_channels, dst_bitrate_kbps, "
            "subtitle_codec, bandwidth_kbps, quality_profile) VALUES "
            "(?,?,'live',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["server_id"], group_id, row["session_id"], row["user_id"], started, stopped_at,
             paused, play_s, row["start_offset_s"], offset, duration, pct, watched,
             row["buffer_count"], s.media_type, s.rating_key, s.parent_rating_key,
             s.grandparent_rating_key, s.guid, s.grandparent_guid, s.library_id, s.title,
             s.parent_title, s.grandparent_title, s.season_index, s.episode_index, s.year, s.poster,
             s.player_title, s.platform, s.product, s.device, s.machine_id, s.ip_address,
             int(s.is_local), int(s.is_relayed), int(s.is_secure), st.decision, st.video_decision,
             st.audio_decision, st.subtitle_decision, json.dumps(st.transcode_reasons),
             int(st.hw_decode), int(st.hw_encode), src.container, src.video_codec, src.resolution,
             src.dynamic_range, src.audio_codec, src.audio_channels, src.bitrate_kbps,
             st.dst_container, st.dst_video_codec, st.dst_resolution, st.dst_audio_codec,
             st.dst_audio_channels, st.dst_bitrate_kbps, st.subtitle_codec, s.bandwidth_kbps, None),
        )
        play_id = cur.lastrowid
        if s.media_type == "episode":
            binges.on_play(self.conn, play_id)
        events.append(("play_stop", {"session": s.to_dict(), "play_id": play_id, "play_s": play_s,
                                     "percent_complete": pct, "watched": bool(watched)}))
        return play_id

    def _find_group(self, user_id: int, s: PlexSession, started: int, start_offset: int,
                    cfg: dict) -> int | None:
        window = int(cfg["ingest.group_resume_h"]) * 3600
        if window <= 0:
            return None
        if s.guid:
            match, val = "guid = ?", s.guid
        elif s.rating_key:
            match, val = "rating_key = ?", s.rating_key
        else:
            return None
        prev = self.conn.execute(
            f"SELECT id, group_id, view_offset_s FROM plays WHERE user_id = ? AND {match} "
            "AND stopped_at <= ? AND stopped_at >= ? AND watched = 0 ORDER BY stopped_at DESC LIMIT 1",
            (user_id, val, started, started - window),
        ).fetchone()
        if prev is None or prev["view_offset_s"] is None:
            return None
        if abs(prev["view_offset_s"] - start_offset) > GROUP_OFFSET_TOLERANCE_S:
            return None
        return prev["group_id"] or prev["id"]


def _watched_threshold(cfg: dict, media_type: str) -> int:
    return int(cfg.get(f"ingest.watched_pct.{media_type}", cfg.get("ingest.watched_pct.episode", 85)))
