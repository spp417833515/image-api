"""A scripted installation and scripted checks for the upgrade-flow tests (no git, uv, systemd or ComfyUI)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from imagegen.biz.version_state import Upgrading, Version, VersionState
from imagegen.biz.versions import VersionManager


@dataclass(frozen=True)
class Tagged:
    tag: str | None
    commit: str


BASE = Tagged(None, "0b05cd8a")  # the master commit the installation starts on
V425 = Tagged("v0.42.5", "483dae43")
V424 = Tagged("v0.42.4", "3f438ca6")


class ProcessKilled(BaseException):
    """Stands for kill -9: nothing in the flow may handle it (the record it leaves is what a real kill leaves)."""


class FakeInstall:
    """Records every call in order ("checkout 483dae43", "restore comfyui==0b05cd8a", …).

    failures[call]: raised (once) when that exact call is made. freeze() gives a snapshot naming the checked-out
    commit, so restore calls show which snapshot was restored. HEAD starts on branch master (at BASE).
    """

    def __init__(self, state: VersionState, head: Tagged = BASE) -> None:
        self.state = state
        self.head = head
        self.branches = {"master": BASE.commit}
        self.on_branch: str | None = "master" if head == BASE else None
        self.calls: list[str] = []
        self.failures: dict[str, BaseException] = {}
        self.dirty = False
        self.online = True
        self.upgrading_at_stop: list[Upgrading | None] = []
        self._known = {t.commit: t for t in (BASE, V425, V424)}

    def _call(self, call: str) -> None:
        self.calls.append(call)
        if call in self.failures:
            raise self.failures.pop(call)

    def installed(self) -> Tagged:
        return self.head

    def fetch_releases(self) -> None:
        self._call("fetch")

    def releases(self) -> list[Tagged]:
        return [V425, V424]

    def require_clean(self) -> None:
        self._call("require_clean")
        if self.dirty:
            raise RuntimeError("ComfyUI 的工作区有改动\n M nodes.py")

    def branch(self) -> str | None:
        return self.on_branch

    def checkout(self, commit: str, branch: str | None = None) -> str | None:
        self._call(f"checkout {commit}")
        self.head = self._known[commit]
        self.on_branch = branch if branch is not None and self.branches.get(branch) == commit else None
        return self.on_branch

    def freeze(self) -> str:
        self._call("freeze")
        return f"comfyui=={self.head.commit}\ntorch==2.14.1+cu130\n"

    def sync_requirements(self) -> None:
        self._call("sync")

    def restore(self, snapshot: Path) -> None:
        self._call(f"restore {snapshot.read_text().splitlines()[0]}")

    def stop(self) -> None:
        self._call("stop")
        self.upgrading_at_stop.append(self.state.upgrading())

    def start(self) -> None:
        self._call("start")

    async def is_online(self) -> bool:
        return self.online

    async def wait_idle(self, timeout_s: float) -> None:
        self._call("wait_idle")


class WatchedState(VersionState):
    """Notes, each time upgrading.json is written, whether the origin's snapshot was already stored; with
    die_after_marking, the process "dies" right after writing it."""

    def __init__(self, directory: Path, die_after_marking: bool = False) -> None:
        super().__init__(directory)
        self.die_after_marking = die_after_marking
        self.snapshot_stored_when_marked: list[bool] = []

    def set_upgrading(self, state: Upgrading, origin: Version | None = None) -> None:
        self.snapshot_stored_when_marked.append(origin is not None and self.snapshot(origin) is not None)
        super().set_upgrading(state, origin)
        if self.die_after_marking:
            raise ProcessKilled


class ScriptedCheck:
    """Passes, except on the runs listed in failures (0-based run number → exception)."""

    def __init__(self, name: str, label: str, failures: dict[int, Exception] | None = None) -> None:
        self.name = name
        self.label = label
        self.failures = failures or {}
        self.runs = 0

    async def run(self) -> None:
        run, self.runs = self.runs, self.runs + 1
        if run in self.failures:
            raise self.failures[run]


def scripted_checks(**failures: dict[int, Exception]) -> list[ScriptedCheck]:
    """starts / nodes / routes, as the real ones; e.g. scripted_checks(routes={0: CheckFailed("…")})."""
    named = (("starts", "能启动"), ("nodes", "节点齐全"), ("routes", "三条路试跑"))
    return [ScriptedCheck(name, label, failures.get(name)) for name, label in named]


def manager(
    tmp_path: Path,
    checks: list[ScriptedCheck] | None = None,
    head: Tagged = BASE,
    state: VersionState | None = None,
    install: FakeInstall | None = None,
) -> tuple[VersionManager, FakeInstall, VersionState, list[str]]:
    state = state or VersionState.in_data_dir(tmp_path)
    install = install or FakeInstall(state, head)
    reported: list[str] = []
    return VersionManager(install, state, checks or scripted_checks(), reported.append), install, state, reported


def kill_recorder(state: VersionState) -> None:
    """Make upgrading.json look written by a process that has exited (its recorded start time no longer matches)."""
    record = json.loads(state.upgrading_path.read_text())
    record["pid_start"] = -1
    state.upgrading_path.write_text(json.dumps(record))
