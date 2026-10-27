"""The parts of imagegen.biz.version_state the upgrade flow added: live vs interrupted upgrades, the lock, dependency
snapshots. (The original files and models are tested in tests/unit/biz/test_biz_version_state.py.)"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from imagegen.biz.version_state import Interrupted, UpgradeBusy, Upgrading, Version, VersionState, _process_start
from versions_fakes import kill_recorder

AT = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
ORIGIN = Version(tag=None, commit="0b05cd8a")


def test_an_upgrade_of_a_live_process_blocks_as_upgrading(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    state.set_upgrading(Upgrading(target="v0.42.5", since=AT), origin=ORIGIN)
    on_disk = json.loads(state.upgrading_path.read_text())
    assert on_disk["pid"] == os.getpid() and on_disk["origin"] == {"tag": None, "commit": "0b05cd8a"}
    assert state.upgrading() == Upgrading(target="v0.42.5", since=AT)
    assert state.generation_block() == Upgrading(target="v0.42.5", since=AT)
    assert state.interrupted() is None


def test_an_upgrade_whose_process_exited_reads_as_interrupted(tmp_path) -> None:
    writer = textwrap.dedent(f"""
        from datetime import datetime, UTC
        from pathlib import Path
        from imagegen.biz.version_state import Upgrading, Version, VersionState
        state = VersionState.in_data_dir(Path({str(tmp_path)!r}))
        state.set_upgrading(Upgrading(target="v0.42.5", since=datetime(2026, 10, 26, 12, tzinfo=UTC)),
                            origin=Version(tag=None, commit="0b05cd8a"))
    """)
    subprocess.run([sys.executable, "-c", writer], check=True)
    state = VersionState.in_data_dir(tmp_path)
    assert state.upgrading() is None
    interrupted = state.interrupted()
    assert interrupted == state.generation_block()
    assert isinstance(interrupted, Interrupted) and interrupted.origin == ORIGIN and interrupted.target == "v0.42.5"


def test_a_reused_pid_is_not_mistaken_for_the_upgrade(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    state.set_upgrading(Upgrading(target="v0.42.5", since=AT), origin=ORIGIN)
    kill_recorder(state)  # same pid (this process), other start time
    assert state.upgrading() is None and state.interrupted().pid == os.getpid()


def test_clearing_ends_both(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    state.set_upgrading(Upgrading(target="v0.42.5", since=AT))
    kill_recorder(state)
    state.clear_upgrading()
    assert state.generation_block() is None


def test_snapshots_are_found_by_commit_whatever_its_abbreviation(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    assert state.snapshot(ORIGIN) is None
    short = Version(tag=None, commit="0b05cd8")
    first = state.save_snapshot(short, "torch==2.14.1+cu130\n")
    assert state.snapshot(ORIGIN) == first and first.parent == state.snapshots_dir
    replaced = state.save_snapshot(ORIGIN, "torch==2.14.1+cu130\nfoo==2\n")
    assert sorted(state.snapshots_dir.iterdir()) == [replaced]  # one snapshot per version
    assert state.snapshot(short).read_text().endswith("foo==2\n")
    assert state.snapshot(Version(tag="v0.42.5", commit="483dae43")) is None
    with pytest.raises(ValueError, match="empty"):
        state.save_snapshot(ORIGIN, "\n")


def test_the_lock_is_exclusive_and_says_what_runs(tmp_path) -> None:
    state = VersionState.in_data_dir(tmp_path)
    other = VersionState.in_data_dir(tmp_path)
    with state.lock():
        state.set_upgrading(Upgrading(target="v0.42.5", since=AT))
        with pytest.raises(UpgradeBusy, match="正在切换到 v0.42.5"), other.lock():
            pass
    with other.lock():  # released at the end of the block
        pass


def test_versions_compare_by_commit_and_show_their_tag() -> None:
    assert Version(tag=None, commit="0b05cd8").same_commit(ORIGIN) and ORIGIN.same_commit(
        Version(tag=None, commit="0b05cd8")
    )
    assert not ORIGIN.same_commit(Version(tag=None, commit="0b05cd8b"))
    assert (ORIGIN.label, Version(tag="v0.42.5", commit="483dae43").label) == ("0b05cd8a", "v0.42.5")


def test_the_process_start_time_is_read_from_the_right_proc_field() -> None:
    """starttime (field 22) converts to the wall-clock time the process started; neighbouring fields do not."""
    btime = next(
        int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime")
    )
    spawned = time.time()
    child = subprocess.Popen(["sleep", "30"])
    try:
        started = btime + _process_start(child.pid) / os.sysconf("SC_CLK_TCK")
        assert abs(started - spawned) < 2.0
        assert _process_start(os.getpid()) is not None
    finally:
        child.kill()
        child.wait()
    assert _process_start(child.pid) is None  # reaped: no such process
