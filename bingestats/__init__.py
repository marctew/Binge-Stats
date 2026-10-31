"""Binge Stats: self-hosted Plex monitoring and statistics."""
from __future__ import annotations

__version__ = "0.1.0"


def create_app(data_dir: str | None = None, *, testing: bool = False):
    from .web.app import build_app
    return build_app(data_dir, testing=testing)
