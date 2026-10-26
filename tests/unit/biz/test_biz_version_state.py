"""The version state files (single owner: imagegen.biz.version_state) and reading the installed ComfyUI version
(imagegen.logic.comfy.version) from a real local git repository."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from imagegen.biz.version_state import (
    CheckItem,
    LastCheck,
    Operation,
    OperationKind,
    OperationResult,
    Upgrading,
    Version,
    VersionState,
    VersionStateError,
)
from imagegen.logic.comfy.version import InstalledVersion, VersionReadError, read_installed_version
from pydantic import ValidationError

AT = datetime(2026, 10, 26, 20, 0, tzinfo=timezone(timedelta(hours=8)))
OLD = Version(tag=None, commit="0b05cd8")
NEW = Version(tag="v0.42.5", commit="483dae4")


def operation(kind: OperationKind, result: OperationResult, note: str | None = None) -> Operation:
    return Operation(at=AT, kind=kind, from_=OLD, to=NEW, result=result, note=note)


def test_nothing_recorded_reads_as_none(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    assert state.directory == tmp_path / "comfy"
    assert (state.upgrading(), state.last_check(), state.last_operation(), state.operations()) == (None, None, None, [])


def test_upgrading_is_written_and_cleared(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    state.set_upgrading(Upgrading(target="v0.42.5", since=AT))
    assert state.upgrading() == Upgrading(target="v0.42.5", since=AT)
    assert json.loads(state.upgrading_path.read_text())["since"] == "2026-10-26T20:00:00+08:00"
    state.clear_upgrading()
    state.clear_upgrading()  # clearing twice is fine (also after a crashed upgrade)
    assert state.upgrading() is None


def test_last_check_round_trips(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    items = [
        CheckItem(name="starts", label="能启动", ok=True),
        CheckItem(name="routes", label="三条路试跑", ok=False, note="Value 0 smaller than min of 1"),
    ]
    state.set_last_check(LastCheck(at=AT, version=NEW, items=items))
    assert state.last_check() == LastCheck(at=AT, version=NEW, items=items)


def test_operations_append_in_order_with_from_on_the_wire(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    first = operation(OperationKind.UPGRADE, OperationResult.PASSED)
    second = operation(OperationKind.ROLLBACK, OperationResult.REVERTED, note="三条路试跑：…")
    state.append_operation(first)
    state.append_operation(second)
    assert state.operations() == [first, second] and state.last_operation() == second
    lines = state.operations_path.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["from"] == {"tag": None, "commit": "0b05cd8"}


def test_models_refuse_what_the_page_cannot_show() -> None:
    with pytest.raises(ValidationError):
        LastCheck(at=AT, version=NEW, items=[])  # at least one check item
    with pytest.raises(ValidationError):
        Version(tag="v1", commit="")  # commit is never empty
    with pytest.raises(ValidationError):
        Upgrading(target="v0.42.5", since=datetime(2026, 10, 26))  # times carry a time zone
    with pytest.raises(ValidationError):
        Operation.model_validate({"at": AT, "kind": "upgrade", "from": OLD, "to": NEW, "result": "maybe"})


def test_a_corrupt_state_file_fails_loudly_naming_it(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    state.directory.mkdir(parents=True)
    state.upgrading_path.write_text('{"target": "v1"}')
    with pytest.raises(VersionStateError, match="upgrading.json"):
        state.upgrading()
    state.operations_path.write_text(
        operation(OperationKind.UPGRADE, OperationResult.PASSED).model_dump_json() + "\nnope\n"
    )
    with pytest.raises(VersionStateError, match="line 2"):
        state.operations()


def git(root: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env={**os.environ, **env})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_installed_version_from_git(tmp_path) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "one")
    commit = git(tmp_path, "rev-parse", "--short", "HEAD")
    assert read_installed_version(tmp_path) == InstalledVersion(None, commit)  # no tags at all
    git(tmp_path, "tag", "v0.1.0")
    assert read_installed_version(tmp_path) == InstalledVersion("v0.1.0", commit)
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "two")
    later = git(tmp_path, "rev-parse", "--short", "HEAD")
    assert read_installed_version(tmp_path) == InstalledVersion(None, later)  # tags exist, none on HEAD
    assert Version.model_validate(read_installed_version(tmp_path), from_attributes=True) == Version(
        tag=None, commit=later
    )


def test_not_a_git_checkout_fails_loudly(tmp_path) -> None:
    with pytest.raises(VersionReadError, match="rev-parse"):
        read_installed_version(tmp_path / "missing")


def test_aware_times_serialise_as_iso_with_zone() -> None:
    wire = Upgrading(target="v1", since=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)).model_dump(mode="json")
    assert wire["since"] == "2026-01-02T03:04:05Z"
