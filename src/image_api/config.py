"""Where image-api listens and keeps its data. Defaults, overridable by environment variables (and serve's options)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOST_ENV = "IMAGE_API_HOST"
PORT_ENV = "IMAGE_API_PORT"
DATA_DIR_ENV = "IMAGE_API_DATA_DIR"


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"  # local only: there is no login
    port: int = 8190
    data_dir: Path = Path("data")  # images and version state; relative to the working directory

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Settings:
        defaults = cls()
        port = environ.get(PORT_ENV)
        if port is not None and not port.isdigit():
            raise ValueError(f"{PORT_ENV} must be a port number, got {port!r}")
        return cls(
            host=environ.get(HOST_ENV, defaults.host),
            port=defaults.port if port is None else int(port),
            data_dir=Path(environ[DATA_DIR_ENV]) if DATA_DIR_ENV in environ else defaults.data_dir,
        )
