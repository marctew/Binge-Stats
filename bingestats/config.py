"""Bootstrap config: /data/config.toml.

Only what's needed before the database is open lives here. Everything else
is a setting in the database (services/settings.py).
"""
from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data_dir: Path
    secret_key: str
    log_level: str = "INFO"
    trusted_proxies: list[str] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bingestats.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def _toml_value(v) -> str:
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return str(v)


def load(data_dir: str | os.PathLike | None = None) -> Config:
    data_dir = Path(data_dir or os.environ.get("DATA_DIR", "/data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "config.toml"
    if not path.exists():
        values = {
            "secret_key": secrets.token_urlsafe(48),
            "log_level": "INFO",
            "trusted_proxies": [],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            "# Binge Stats bootstrap config. Everything else is in Settings in the UI.\n"
            + "".join(f"{k} = {_toml_value(v)}\n" for k, v in values.items())
        )
        os.chmod(tmp, 0o600)
        try:
            # Two processes may start together: only the first one's file wins.
            os.link(tmp, path)
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)
    raw = tomllib.loads(path.read_text())
    return Config(
        data_dir=data_dir,
        secret_key=raw["secret_key"],
        log_level=raw.get("log_level", "INFO"),
        trusted_proxies=list(raw.get("trusted_proxies", [])),
    )
