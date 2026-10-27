"""image-api's command line and settings (the real server start is in tests/live/test_api_live.py)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from image_api import cli
from image_api.config import ConfigError, data_dir_from_env, fill_from_env, host_from_env, port_from_env

HOME = Path("/home/someone")


@pytest.fixture
def clean_env(monkeypatch, tmp_path) -> Path:
    """No image-api / XDG variables from the machine running the tests; HOME is a fresh directory."""
    for name in ("IMAGE_API_HOST", "IMAGE_API_PORT", "IMAGE_API_DATA_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def settings(environ: dict[str, str]) -> tuple[Path, str, int]:
    return data_dir_from_env(environ, HOME), host_from_env(environ), port_from_env(environ)


def test_defaults_and_environment() -> None:
    assert settings({}) == (HOME / ".local/share/image-api", "127.0.0.1", 8190)
    env = {"IMAGE_API_HOST": "0.0.0.0", "IMAGE_API_PORT": "9000", "IMAGE_API_DATA_DIR": "/srv/images"}
    assert settings(env) == (Path("/srv/images"), "0.0.0.0", 9000)
    with pytest.raises(ConfigError, match="IMAGE_API_PORT"):
        port_from_env({"IMAGE_API_PORT": "eighty"})


def test_only_options_the_command_line_left_unset_are_filled_from_the_environment() -> None:
    env = {"IMAGE_API_DATA_DIR": "/srv/images", "IMAGE_API_HOST": "0.0.0.0", "IMAGE_API_PORT": "9000"}
    options = argparse.Namespace(data_dir=None, host="10.0.0.1", port=None, other=None)
    fill_from_env(options, env)
    assert vars(options) == {"data_dir": Path("/srv/images"), "host": "10.0.0.1", "port": 9000, "other": None}
    given = argparse.Namespace(data_dir=Path("/given"))  # a command without --host / --port gets none added
    fill_from_env(given, {"XDG_DATA_HOME": "relative/dir", "IMAGE_API_PORT": "eighty"})  # neither is read
    assert vars(given) == {"data_dir": Path("/given")}
    with pytest.raises(ConfigError, match="XDG_DATA_HOME"):
        fill_from_env(argparse.Namespace(data_dir=None), {"XDG_DATA_HOME": "relative/dir"})


def test_the_data_directory_follows_xdg_and_can_be_overridden() -> None:
    assert data_dir_from_env({"XDG_DATA_HOME": "/data/xdg"}, HOME) == Path("/data/xdg/image-api")
    assert data_dir_from_env({"XDG_DATA_HOME": ""}, HOME) == HOME / ".local/share/image-api"
    both = {"XDG_DATA_HOME": "/data/xdg", "IMAGE_API_DATA_DIR": "/srv/images"}
    assert data_dir_from_env(both, HOME) == Path("/srv/images")
    with pytest.raises(ConfigError, match="XDG_DATA_HOME 要是绝对路径"):
        data_dir_from_env({"XDG_DATA_HOME": "relative/dir"}, HOME)


def test_the_data_directory_is_the_same_absolute_one_from_any_working_directory(monkeypatch, tmp_path) -> None:
    seen = set()
    for cwd in (tmp_path, tmp_path / "a" / "b"):
        cwd.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(cwd)
        seen.add(data_dir_from_env({}, HOME))
        seen.add(data_dir_from_env({"XDG_DATA_HOME": "/data/xdg/../xdg"}, HOME))
    assert seen == {HOME / ".local/share/image-api", Path("/data/xdg/image-api")}


def test_a_relative_override_is_made_absolute_when_read(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    assert data_dir_from_env({"IMAGE_API_DATA_DIR": "mine/../images"}, HOME) == tmp_path / "images"
    assert data_dir_from_env({"IMAGE_API_DATA_DIR": "~/pics"}, HOME) == Path.home() / "pics"


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


UNUSABLE = {"XDG_DATA_HOME": "relative/dir", "IMAGE_API_PORT": "eighty"}


@pytest.mark.parametrize(
    "command", [["--help"], ["serve", "--help"], ["comfy", "--help"], ["comfy", "versions", "--help"]]
)
def test_help_works_whatever_the_environment(clean_env, monkeypatch, capsys, command) -> None:
    for name, value in UNUSABLE.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as done:
        cli.main(command)
    assert done.value.code == 0 and "usage: image-api" in capsys.readouterr().out


def test_explicit_settings_win_over_unusable_environment_variables(clean_env, monkeypatch) -> None:
    for name, value in UNUSABLE.items():
        monkeypatch.setenv(name, value)
    serve = cli.build_parser().parse_args(["serve", "--data-dir", "/srv/a", "--port", "8200"])
    assert (serve.data_dir, serve.port) == (Path("/srv/a"), 8200)
    assert cli.build_parser().parse_args(["comfy", "rollback", "--data-dir", "/srv/a"]).data_dir == Path("/srv/a")
    monkeypatch.setenv("IMAGE_API_DATA_DIR", "/srv/b")  # also wins over XDG_DATA_HOME, which is then never read
    assert cli.build_parser().parse_args(["comfy", "versions"]).data_dir == Path("/srv/b")


@pytest.mark.parametrize(
    ("variable", "value", "command", "says"),
    [
        ("XDG_DATA_HOME", "relative/dir", ["serve", "--port", "8200"], "XDG_DATA_HOME 要是绝对路径"),
        ("XDG_DATA_HOME", "relative/dir", ["comfy", "versions"], "XDG_DATA_HOME 要是绝对路径"),
        ("IMAGE_API_PORT", "eighty", ["serve", "--data-dir", "/srv/a"], "IMAGE_API_PORT 要是端口号"),
    ],
)
def test_a_needed_but_unusable_setting_is_a_one_line_error(
    clean_env, monkeypatch, capsys, variable, value, command, says
):
    monkeypatch.setenv(variable, value)
    with pytest.raises(SystemExit) as done:
        cli.main(command)
    error = capsys.readouterr().err.strip().splitlines()
    assert done.value.code == 2
    assert error[-1].startswith(f"image-api {command[0]}") and says in error[-1] and value in error[-1]
    assert error[0].startswith("usage:") and len(error) <= 3  # argparse's usage line(s) and the one error line


def test_the_installed_command_reports_it_without_a_traceback(clean_env, tmp_path) -> None:
    command = str(Path(sys.executable).parent / "image-api")
    env = {**os.environ, "XDG_DATA_HOME": "relative/dir"}
    shown = subprocess.run([command, "serve", "--help"], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert shown.returncode == 0 and "--data-dir" in shown.stdout
    refused = subprocess.run([command, "serve"], env=env, capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert refused.returncode == 2 and "Traceback" not in refused.stderr
    assert "XDG_DATA_HOME 要是绝对路径" in refused.stderr.strip().splitlines()[-1]
