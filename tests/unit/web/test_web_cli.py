"""image-api's command line and settings (the real server start is in tests/live/test_api_live.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from image_api import cli
from image_api.config import Settings

HOME = Path("/home/someone")


@pytest.fixture
def clean_env(monkeypatch, tmp_path) -> Path:
    """No image-api / XDG variables from the machine running the tests; HOME is a fresh directory."""
    for name in ("IMAGE_API_HOST", "IMAGE_API_PORT", "IMAGE_API_DATA_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_settings_defaults_and_environment() -> None:
    assert Settings.from_env({}, home=HOME) == Settings(HOME / ".local/share/image-api", "127.0.0.1", 8190)
    env = {"IMAGE_API_HOST": "0.0.0.0", "IMAGE_API_PORT": "9000", "IMAGE_API_DATA_DIR": "/srv/images"}
    assert Settings.from_env(env, home=HOME) == Settings(Path("/srv/images"), "0.0.0.0", 9000)
    with pytest.raises(ValueError, match="IMAGE_API_PORT"):
        Settings.from_env({"IMAGE_API_PORT": "eighty"}, home=HOME)


def test_the_data_directory_follows_xdg_and_can_be_overridden() -> None:
    assert Settings.from_env({"XDG_DATA_HOME": "/data/xdg"}, home=HOME).data_dir == Path("/data/xdg/image-api")
    assert Settings.from_env({"XDG_DATA_HOME": ""}, home=HOME).data_dir == HOME / ".local/share/image-api"
    both = {"XDG_DATA_HOME": "/data/xdg", "IMAGE_API_DATA_DIR": "/srv/images"}
    assert Settings.from_env(both, home=HOME).data_dir == Path("/srv/images")
    with pytest.raises(ValueError, match="XDG_DATA_HOME must be an absolute path"):
        Settings.from_env({"XDG_DATA_HOME": "relative/dir"}, home=HOME)


def test_the_data_directory_is_the_same_absolute_one_from_any_working_directory(monkeypatch, tmp_path) -> None:
    seen = set()
    for cwd in (tmp_path, tmp_path / "a" / "b"):
        cwd.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(cwd)
        seen.add(Settings.from_env({}, home=HOME).data_dir)
        seen.add(Settings.from_env({"XDG_DATA_HOME": "/data/xdg/../xdg"}, home=HOME).data_dir)
    assert seen == {HOME / ".local/share/image-api", Path("/data/xdg/image-api")}


def test_a_relative_override_is_made_absolute_when_read(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    assert Settings.from_env({"IMAGE_API_DATA_DIR": "mine/../images"}, home=HOME).data_dir == tmp_path / "images"
    assert Settings.from_env({"IMAGE_API_DATA_DIR": "~/pics"}, home=HOME).data_dir == Path.home() / "pics"


def test_serve_and_comfy_share_the_default_data_directory(clean_env, monkeypatch, tmp_path) -> None:
    expected = clean_env / ".local/share/image-api"
    for cwd in (tmp_path, Path("/")):
        monkeypatch.chdir(cwd)
        serve = cli.build_parser().parse_args(["serve"]).data_dir
        comfy = [cli.build_parser().parse_args(["comfy", *action]).data_dir for action in (["versions"], ["rollback"])]
        assert serve == expected and comfy == [expected, expected]
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    serve = cli.build_parser().parse_args(["serve"]).data_dir
    assert serve == cli.build_parser().parse_args(["comfy", "recover"]).data_dir == tmp_path / "xdg/image-api"


def test_serve_takes_its_defaults_from_the_environment(clean_env, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IMAGE_API_PORT", "8191")
    args = cli.build_parser().parse_args(["serve"])
    expected = (clean_env / ".local/share/image-api", "127.0.0.1", 8191, cli.run_serve)
    assert (args.data_dir, args.host, args.port, args.run) == expected
    monkeypatch.chdir(tmp_path)
    for command in (["serve", "--port", "8200"], ["comfy", "versions"]):
        args = cli.build_parser().parse_args([*command, "--data-dir", "elsewhere"])
        assert args.data_dir == tmp_path / "elsewhere"  # a relative --data-dir is made absolute at once


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_subcommands_register_through_one_table(monkeypatch) -> None:
    def register_hello(commands) -> None:
        hello = commands.add_parser("hello")
        hello.set_defaults(run=lambda args: 7)

    monkeypatch.setattr(cli, "SUBCOMMANDS", (*cli.SUBCOMMANDS, register_hello))
    assert cli.main(["hello"]) == 7
