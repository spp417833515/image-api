"""Where image-api listens and keeps its data: the single source of these defaults (serve and the comfy commands both
read them here), overridable by environment variables and command-line options.

The data directory (images, ComfyUI version state, the upgrade lock) is per user, not per working directory, so
`image-api serve` and `image-api comfy …` see the same one wherever they are started (ppwiki decision id=8):
$IMAGE_API_DATA_DIR, else $XDG_DATA_HOME/image-api, else ~/.local/share/image-api. Always an absolute path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOST_ENV = "IMAGE_API_HOST"
PORT_ENV = "IMAGE_API_PORT"
DATA_DIR_ENV = "IMAGE_API_DATA_DIR"
XDG_DATA_HOME_ENV = "XDG_DATA_HOME"
APP_DIR = "image-api"
DEFAULT_HOST = "127.0.0.1"  # local only: there is no login
DEFAULT_PORT = 8190


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path  # absolute
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ, home: Path | None = None) -> Settings:
        """home: the user's home directory (default Path.home())."""
        port = environ.get(PORT_ENV)
        if port is not None and not port.isdigit():
            raise ValueError(f"{PORT_ENV} must be a port number, got {port!r}")
        explicit = environ.get(DATA_DIR_ENV)
        return cls(
            data_dir=data_dir_arg(explicit) if explicit else default_data_dir(environ, home or Path.home()),
            host=environ.get(HOST_ENV, DEFAULT_HOST),
            port=DEFAULT_PORT if port is None else int(port),
        )


def default_data_dir(environ: Mapping[str, str], home: Path) -> Path:
    """$XDG_DATA_HOME/image-api, or ~/.local/share/image-api when XDG_DATA_HOME is unset or empty."""
    xdg = environ.get(XDG_DATA_HOME_ENV)
    if not xdg:
        return data_dir_arg(str(home / ".local" / "share" / APP_DIR))
    if not Path(xdg).is_absolute():
        # The XDG spec calls a relative path invalid; refuse it rather than guess a directory.
        raise ValueError(f"{XDG_DATA_HOME_ENV} must be an absolute path, got {xdg!r}")
    return data_dir_arg(str(Path(xdg) / APP_DIR))


def data_dir_arg(value: str) -> Path:
    """A data directory as given (environment variable or --data-dir): ~ expanded, made absolute and normalised
    against the current directory, so every later use means the same directory."""
    return Path(os.path.abspath(Path(value).expanduser()))
