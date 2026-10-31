"""Plain dataclasses the rest of the app uses instead of Plex JSON."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class SourceMedia:
    """What the file on disk is."""
    container: str | None = None
    video_codec: str | None = None
    resolution: str | None = None
    dynamic_range: str | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    bitrate_kbps: int | None = None
    size_bytes: int | None = None


@dataclass
class StreamInfo:
    """How a session is being delivered."""
    decision: str = "direct_play"          # direct_play | direct_stream | transcode
    video_decision: str | None = None      # direct play | copy | transcode
    audio_decision: str | None = None
    subtitle_decision: str | None = None   # burn | transcode | copy | None
    transcode_reasons: list[str] = field(default_factory=list)
    hw_decode: bool = False
    hw_encode: bool = False
    throttled: bool = False
    speed: float | None = None
    src: SourceMedia = field(default_factory=SourceMedia)
    src_from_library: bool = False         # True once filled from /library/metadata
    dst_container: str | None = None
    dst_video_codec: str | None = None
    dst_resolution: str | None = None
    dst_dynamic_range: str | None = None
    dst_audio_codec: str | None = None
    dst_audio_channels: int | None = None
    dst_bitrate_kbps: int | None = None
    subtitle_codec: str | None = None
    has_transcode_session: bool = False


@dataclass
class PlexSession:
    session_id: str
    session_key: str | None
    user_id: int
    user_title: str | None
    user_thumb: str | None
    media_type: str
    rating_key: int | None
    parent_rating_key: int | None
    grandparent_rating_key: int | None
    guid: str | None
    grandparent_guid: str | None
    title: str | None
    parent_title: str | None
    grandparent_title: str | None
    season_index: int | None
    episode_index: int | None
    year: int | None
    thumb: str | None
    parent_thumb: str | None
    grandparent_thumb: str | None
    art: str | None
    library_id: int | None
    library_title: str | None
    duration_s: int | None
    view_offset_s: int | None
    player_title: str | None
    platform: str | None
    product: str | None
    device: str | None
    machine_id: str | None
    state: str                              # playing | paused | buffering
    ip_address: str | None
    is_local: bool
    is_relayed: bool
    is_secure: bool
    bandwidth_kbps: int | None
    location: str | None                    # lan | wan | cellular
    media_id: str | None
    stream: StreamInfo

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlexSession":
        d = dict(d)
        s = dict(d.pop("stream", {}) or {})
        src = SourceMedia(**_known(SourceMedia, s.pop("src", {}) or {}))
        stream = StreamInfo(src=src, **_known(StreamInfo, s, skip={"src"}))
        return cls(stream=stream, **_known(cls, d, skip={"stream"}))

    @property
    def poster(self) -> str | None:
        """Best artwork for a card: show poster for episodes, album for tracks."""
        if self.media_type == "episode":
            return self.grandparent_thumb or self.parent_thumb or self.thumb
        if self.media_type == "track":
            return self.parent_thumb or self.thumb
        return self.thumb


def _known(cls, d: dict, skip: set[str] = frozenset()) -> dict:
    names = {f.name for f in fields(cls)} - set(skip)
    return {k: v for k, v in d.items() if k in names}


@dataclass
class ServerIdentity:
    machine_id: str
    name: str | None
    version: str | None
