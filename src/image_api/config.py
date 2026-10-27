"""Where image-api listens and keeps its data: the single source of these defaults (serve and the comfy commands both
read them here), overridable by environment variables and command-line options.

The data directory (images, ComfyUI version state, the upgrade lock) is per user, not per working directory, so
`image-api serve` and `image-api comfy …` see the same one wherever they are started (ppwiki decision id=8):
$IMAGE_API_DATA_DIR, else $XDG_DATA_HOME/image-api, else ~/.local/share/image-api. Always an absolute path.
The command line reads the environment only for options it was not given (fill_from_env), after parsing: --help
always works, and an explicit option is never blocked by an unusable environment variable.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from pathlib import Path

HOST_ENV = "IMAGE_API_HOST"
PORT_ENV = "IMAGE_API_PORT"
DATA_DIR_ENV = "IMAGE_API_DATA_DIR"
XDG_DATA_HOME_ENV = "XDG_DATA_HOME"
APP_DIR = "image-api"
DEFAULT_HOST = "127.0.0.1"  # local only: there is no login
DEFAULT_PORT = 8190


DATA_DIR_RULE = f"${DATA_DIR_ENV}，否则 ${XDG_DATA_HOME_ENV}/{APP_DIR}，否则 ~/.local/share/{APP_DIR}"


class ConfigError(ValueError):
    """A setting from the environment cannot be used; the message is one line for the user."""


def data_dir_from_env(environ: Mapping[str, str] = os.environ, home: Path | None = None) -> Path:
    """$IMAGE_API_DATA_DIR if set (then XDG_DATA_HOME is not looked at), else default_data_dir()."""
    if explicit := environ.get(DATA_DIR_ENV):
        return data_dir_arg(explicit)
    return default_data_dir(environ, home or Path.home())


def default_data_dir(environ: Mapping[str, str], home: Path) -> Path:
    """$XDG_DATA_HOME/image-api, or ~/.local/share/image-api when XDG_DATA_HOME is unset or empty."""
    xdg = environ.get(XDG_DATA_HOME_ENV)
    if not xdg:
        return data_dir_arg(str(home / ".local" / "share" / APP_DIR))
    if not Path(xdg).is_absolute():
        # The XDG spec calls a relative path invalid; refuse it rather than guess a directory.
        raise ConfigError(
            f"{XDG_DATA_HOME_ENV} 要是绝对路径，现在是 {xdg!r}：改成绝对路径，"
            f"或用 --data-dir / {DATA_DIR_ENV} 指定数据目录"
        )
    return data_dir_arg(str(Path(xdg) / APP_DIR))


def host_from_env(environ: Mapping[str, str] = os.environ) -> str:
    return environ.get(HOST_ENV, DEFAULT_HOST)


def port_from_env(environ: Mapping[str, str] = os.environ) -> int:
    port = environ.get(PORT_ENV)
    if port is None:
        return DEFAULT_PORT
    if not port.isdigit():
        raise ConfigError(f"{PORT_ENV} 要是端口号，现在是 {port!r}")
    return int(port)


def data_dir_arg(value: str) -> Path:
    """A data directory as given (environment variable or --data-dir): ~ expanded, made absolute and normalised
    against the current directory, so every later use means the same directory."""
    return Path(os.path.abspath(Path(value).expanduser()))


_FROM_ENV: dict[str, Callable[[Mapping[str, str]], object]] = {
    "data_dir": data_dir_from_env,
    "host": host_from_env,
    "port": port_from_env,
}


def fill_from_env(options: argparse.Namespace, environ: Mapping[str, str] = os.environ) -> None:
    """Command-line options left unset (None) take their value from the environment or the default.

    Only unset ones: with --data-dir given, $IMAGE_API_DATA_DIR and $XDG_DATA_HOME are not looked at. Raises
    ConfigError when a value that is needed cannot be used.
    """
    for name, read in _FROM_ENV.items():
        if hasattr(options, name) and getattr(options, name) is None:
            setattr(options, name, read(environ))
