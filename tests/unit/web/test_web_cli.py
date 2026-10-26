"""image-api's command line and settings (the real server start is in tests/live/test_api_live.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from image_api import cli
from image_api.config import Settings


def test_settings_defaults_and_environment() -> None:
    assert Settings.from_env({}) == Settings("127.0.0.1", 8190, Path("data"))
    env = {"IMAGE_API_HOST": "0.0.0.0", "IMAGE_API_PORT": "9000", "IMAGE_API_DATA_DIR": "/srv/images"}
    assert Settings.from_env(env) == Settings("0.0.0.0", 9000, Path("/srv/images"))
    with pytest.raises(ValueError, match="IMAGE_API_PORT"):
        Settings.from_env({"IMAGE_API_PORT": "eighty"})


def test_serve_takes_its_defaults_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_API_PORT", "8191")
    args = cli.build_parser().parse_args(["serve"])
    assert (args.host, args.port, args.data_dir, args.run) == ("127.0.0.1", 8191, Path("data"), cli.run_serve)
    args = cli.build_parser().parse_args(["serve", "--port", "8200", "--data-dir", "elsewhere"])
    assert (args.port, args.data_dir) == (8200, Path("elsewhere"))


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_subcommands_register_through_one_table(monkeypatch) -> None:
    def register_hello(commands) -> None:
        hello = commands.add_parser("hello")
        hello.set_defaults(run=lambda args: 7)

    monkeypatch.setattr(cli, "SUBCOMMANDS", (*cli.SUBCOMMANDS, register_hello))
    assert cli.main(["hello"]) == 7
