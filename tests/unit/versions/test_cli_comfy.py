"""`image-api comfy …` over the real flow with a scripted installation: arguments, output and exit codes."""

from __future__ import annotations

import shlex
from datetime import UTC, datetime
from pathlib import Path

import pytest
from imagegen.biz.version_checks import CheckFailed
from imagegen.biz.version_state import RECOVER_COMMAND, Upgrading, VersionState
from imagegen.biz.versions import VersionManager
from versions_fakes import BASE, FakeInstall, kill_recorder, scripted_checks

from image_api import cli, cli_comfy


@pytest.fixture
def comfy(monkeypatch, tmp_path):
    """Runs `image-api comfy …` with the flow on a FakeInstall; returns (run(argv) -> exit code, install, checks)."""
    data_dir = tmp_path / "data"
    state = VersionState.in_data_dir(data_dir)
    install = FakeInstall(state)
    checks = scripted_checks()

    def create(path: Path, report=lambda _: None) -> VersionManager:
        assert path == data_dir
        return VersionManager(install, VersionState.in_data_dir(path), checks, report)

    monkeypatch.setattr(cli_comfy, "create_version_manager", create)
    return (lambda *argv: cli.main(["comfy", *argv, "--data-dir", str(data_dir)])), install, checks


def test_the_recover_hint_is_a_real_command() -> None:
    words = shlex.split(RECOVER_COMMAND)
    assert words[0] == "image-api"
    args = cli.build_parser().parse_args(words[1:])
    assert args.comfy_command == "recover"
    assert "image-api comfy recover" not in Path(cli_comfy.__file__).read_text(encoding="utf-8")  # one source


def test_exit_codes_are_the_documented_numbers() -> None:
    codes = (cli_comfy.EXIT_PASSED, cli_comfy.EXIT_REVERTED, cli_comfy.EXIT_NOT_SWITCHED)
    assert (*codes, cli_comfy.EXIT_NEEDS_RECOVER, cli_comfy.EXIT_UNEXPECTED) == (0, 1, 2, 3, 4)


def test_data_dir_defaults_like_serve(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_API_DATA_DIR", "/srv/images")
    assert cli.build_parser().parse_args(["comfy", "upgrade", "v0.42.5"]).data_dir == Path("/srv/images")


def test_upgrade_passes_with_exit_0_and_prints_each_stage(comfy, capsys) -> None:
    run, install, _ = comfy
    assert run("upgrade", "v0.42.5") == 0
    out = capsys.readouterr().out
    assert "停止 ComfyUI" in out and "检查：三条路试跑" in out
    assert "通过：ComfyUI 现在是 v0.42.5（483dae43）" in out and "✓ 三条路试跑" in out


def test_a_reverted_upgrade_exits_1_naming_the_failed_check(comfy, capsys) -> None:
    run, _, checks = comfy
    checks[2].failures = {0: CheckFailed("文生图：Value 0 smaller than min of 1: steps")}
    assert run("upgrade", "v0.42.5") == 1
    err = capsys.readouterr().err
    assert "v0.42.5（483dae43） 没过，已退回 0b05cd8a（没有标签）" in err
    assert "没过的一项：三条路试跑：文生图：Value 0 smaller than min of 1: steps" in err


def test_a_refused_operation_exits_2(comfy, capsys) -> None:
    run, install, _ = comfy
    assert run("upgrade", "v9.9.9") == 2
    assert "ComfyUI 没有改动：没有正式版 'v9.9.9'；最新的几个：v0.42.5、v0.42.4" in capsys.readouterr().err
    assert run("rollback") == 2
    assert "stop" not in install.calls


def test_a_failed_switch_back_exits_3_with_the_recover_command(comfy, capsys) -> None:
    run, install, checks = comfy
    checks[2].failures = {0: CheckFailed("文生图：boom")}
    install.failures[f"checkout {BASE.commit}"] = RuntimeError("error: unable to unlink old 'nodes.py'")
    assert run("upgrade", "v0.42.5") == 3
    err = capsys.readouterr().err
    assert RECOVER_COMMAND in err and "unable to unlink" in err


def test_an_unexpected_error_exits_4_saying_where_comfyui_is(comfy, capsys, monkeypatch) -> None:
    run, _, _ = comfy

    def disk_full(self, operation) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(VersionState, "append_operation", disk_full)  # after the checks passed
    assert run("upgrade", "v0.42.5") == 4
    err = capsys.readouterr().err
    assert "OSError: [Errno 28] No space left on device" in err  # the traceback
    assert "ComfyUI 现在是 v0.42.5（483dae43），没有检查完；升级标记还在" in err and RECOVER_COMMAND in err


def test_an_unexpected_error_without_a_marker_says_recover_is_not_needed(comfy, capsys, monkeypatch) -> None:
    run, _, _ = comfy

    async def broken(self) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(VersionManager, "rollback", broken)
    assert run("rollback") == 4
    assert "ComfyUI 现在是 0b05cd8a（没有标签），没有留下升级标记（不需要 recover）" in capsys.readouterr().err


def test_errors_before_any_change_exit_2_and_unreadable_versions_exit_4(comfy, capsys) -> None:
    run, install, _ = comfy
    install.failures["wait_idle"] = KeyError("queue_running")  # before ComfyUI is touched: nothing changed
    assert run("upgrade", "v0.42.5") == 2
    capsys.readouterr()
    install.failures["fetch"] = ConnectionResetError("github.com")
    assert run("versions") == 4
    assert (
        "读不到 ComfyUI 的版本（--no-fetch 只看本地已知的版本）：ConnectionResetError: github.com"
        in capsys.readouterr().err
    )


def test_versions_lists_the_current_version_releases_and_history(comfy, capsys) -> None:
    run, install, _ = comfy
    run("upgrade", "v0.42.5")
    capsys.readouterr()
    assert run("versions", "--no-fetch") == 0
    out = capsys.readouterr().out
    assert "fetch" not in install.calls[-1:]
    lines = out.splitlines()
    assert lines[0].startswith("ComfyUI 当前版本：v0.42.5（483dae43） · 检查通过（")
    assert "  ✓ 能启动" in lines
    assert any(
        line.startswith("最近一次操作：") and "升级 0b05cd8a（没有标签） → v0.42.5（483dae43），检查通过" in line
        for line in lines
    )
    assert "可以回滚到：0b05cd8a（没有标签）（image-api comfy rollback）" in lines
    release = next(line for line in lines if line.lstrip().startswith("v0.42.5"))
    assert "检查通过" in release and release.endswith("← 当前")
    assert next(line for line in lines if line.lstrip().startswith("v0.42.4")).endswith("没检查过")
    run("versions")
    assert install.calls[-1] == "fetch"


def test_versions_points_an_interrupted_upgrade_to_recover(comfy, capsys, tmp_path) -> None:
    run, _, _ = comfy
    state = VersionState.in_data_dir(tmp_path / "data")
    state.set_upgrading(Upgrading(target="v0.42.5", since=datetime(2026, 10, 26, tzinfo=UTC)))
    kill_recorder(state)
    run("versions", "--no-fetch")
    assert "上次切换到 v0.42.5 中断了" in capsys.readouterr().out
    assert run("upgrade", "v0.42.4") == 2
    assert RECOVER_COMMAND in capsys.readouterr().err
