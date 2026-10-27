"""The ComfyUI version state files: the single owner of their location and format.

<data_dir>/comfy/
  upgrading.json     present while an upgrade / rollback / recover runs: {"target", "since", "origin", "pid", "pid_start"}
                     If it outlives its process, that operation was interrupted (see interrupted()).
  last_check.json    the checks of the most recent upgrade / rollback: {"at", "version", "items": [...]}
  operations.jsonl   one Operation per line, oldest first
  upgrade.lock       held (flock) by the one process running an upgrade / rollback / recover
  deps/<commit>.txt  the dependency snapshot (uv pip freeze) of each version the flow has left
  deps/<commit>.branch  the branch HEAD was on when the flow left that version (absent: HEAD was detached)

The upgrade flow (another process: `image-api comfy …`) writes them; the service reads them for /api/status and to
refuse generation while an upgrade runs. Writes replace whole files atomically, so a reader never sees half a file.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

# How to repair an interrupted upgrade: the `recover` subcommand of the app's command line.
RECOVER_COMMAND = "image-api comfy recover"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Version(BaseModel):
    """An installed ComfyUI version. Build one from imagegen.logic.comfy.version.InstalledVersion with
    Version.model_validate(installed, from_attributes=True)."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tag: str | None
    commit: str = Field(min_length=1)

    @property
    def label(self) -> str:
        return self.tag or self.commit

    def same_commit(self, other: Version) -> bool:
        """Abbreviations of one commit can differ in length (git lengthens them as the repository grows)."""
        return self.commit.startswith(other.commit) or other.commit.startswith(self.commit)


class CheckItem(BaseModel):
    model_config = _FROZEN

    name: str = Field(min_length=1)  # stable key, e.g. "starts" / "nodes" / "routes"
    label: str = Field(min_length=1)  # shown to people, e.g. "能启动"
    ok: bool
    note: str | None = None  # why it failed (the original error text), None when ok


class LastCheck(BaseModel):
    model_config = _FROZEN

    at: AwareDatetime
    version: Version
    items: list[CheckItem] = Field(min_length=1)


class OperationKind(StrEnum):
    UPGRADE = "upgrade"
    ROLLBACK = "rollback"


class OperationResult(StrEnum):
    PASSED = "passed"
    REVERTED = "reverted"


class Operation(BaseModel):
    # "from" is a Python keyword: the field is from_ in code and "from" on disk and on the wire.
    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    at: AwareDatetime
    kind: OperationKind
    from_: Version = Field(alias="from")
    to: Version
    result: OperationResult
    note: str | None = None  # the check that did not pass and its original text; None when passed


class Upgrading(BaseModel):
    model_config = _FROZEN

    target: str = Field(min_length=1)  # the version being switched to, e.g. "v0.42.5"
    since: AwareDatetime


class Interrupted(BaseModel):
    """An upgrade / rollback / recover whose process is gone without finishing: the installed version is unchecked."""

    model_config = _FROZEN

    target: str = Field(min_length=1)
    since: AwareDatetime
    origin: Version | None  # the version installed when it began: what `recover` returns to
    pid: int


class _UpgradeRecord(BaseModel):
    """upgrading.json: Upgrading plus who is running it, so a reader can tell a live upgrade from an interrupted one."""

    model_config = _FROZEN

    target: str = Field(min_length=1)
    since: AwareDatetime
    origin: Version | None
    pid: int
    pid_start: int  # the process's start time (clock ticks after boot), so a reused pid is not mistaken for it

    def alive(self) -> bool:
        return _process_start(self.pid) == self.pid_start


class VersionStateError(Exception):
    """A state file exists but cannot be read as its model: fail loudly rather than guess."""

    def __init__(self, path: Path, problem: str) -> None:
        super().__init__(f"version state file {path} is unreadable: {problem}")
        self.path = path


class UpgradeBusy(Exception):
    """Another process holds the upgrade lock."""


class VersionState:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.upgrading_path = directory / "upgrading.json"
        self.last_check_path = directory / "last_check.json"
        self.operations_path = directory / "operations.jsonl"
        self.lock_path = directory / "upgrade.lock"
        self.snapshots_dir = directory / "deps"

    @classmethod
    def in_data_dir(cls, data_dir: Path) -> VersionState:
        """The state directory of an image-api data directory (the one the service and the CLI share)."""
        return cls(data_dir / "comfy")

    # ---- read (status, generation guard, the upgrade flow)
    def upgrading(self) -> Upgrading | None:
        """The upgrade running now; None when there is none or its process is gone (see interrupted())."""
        block = self.generation_block()
        return block if isinstance(block, Upgrading) else None

    def interrupted(self) -> Interrupted | None:
        """The upgrade whose process ended without finishing it; None otherwise."""
        block = self.generation_block()
        return block if isinstance(block, Interrupted) else None

    def generation_block(self) -> Upgrading | Interrupted | None:
        """Why generation must wait, from one read of upgrading.json: an upgrade running now, or one interrupted."""
        record = _read(self.upgrading_path, _UpgradeRecord)
        if record is None:
            return None
        if record.alive():
            return Upgrading(target=record.target, since=record.since)
        return Interrupted(target=record.target, since=record.since, origin=record.origin, pid=record.pid)

    def last_check(self) -> LastCheck | None:
        return _read(self.last_check_path, LastCheck)

    def operations(self) -> list[Operation]:
        """Every recorded operation, oldest first."""
        try:
            text = self.operations_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        found = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                found.append(Operation.model_validate_json(line))
            except ValidationError as exc:
                raise VersionStateError(self.operations_path, f"line {number}: {exc}") from exc
        return found

    def last_operation(self) -> Operation | None:
        operations = self.operations()
        return operations[-1] if operations else None

    def snapshot(self, version: Version) -> Path | None:
        """The stored dependency snapshot of version, if there is one."""
        stored = sorted(self.snapshots_dir.glob("*.txt"))  # empty when the directory does not exist yet
        return next((path for path in stored if version.same_commit(Version(tag=None, commit=path.stem))), None)

    def branch_of(self, version: Version) -> str | None:
        """The branch HEAD was on when the flow left version; None if it was detached or version was never left."""
        snapshot = self.snapshot(version)
        if snapshot is None:
            return None
        try:
            return snapshot.with_suffix(".branch").read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None

    # ---- write (only the upgrade / rollback flow)
    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive across processes for the duration of the block; raises UpgradeBusy at once if already held.
        The kernel releases it when the holder exits, however it exits."""
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                running = self.upgrading()
                what = f"（正在切换到 {running.target}，{running.since.isoformat()} 开始）" if running else ""
                raise UpgradeBusy(f"另一个 ComfyUI 升级 / 回滚正在进行{what}，等它结束再试") from None
            yield
        finally:
            os.close(fd)

    def set_upgrading(self, state: Upgrading, origin: Version | None = None) -> None:
        """Record that this process is switching ComfyUI to state.target. origin: the version installed when it began
        (what `recover` returns to if this process dies first); the upgrade flow always gives it."""
        started = _process_start(os.getpid())
        if started is None:
            raise RuntimeError(f"cannot read this process's start time from /proc/{os.getpid()}/stat")
        record = _UpgradeRecord(
            target=state.target, since=state.since, origin=origin, pid=os.getpid(), pid_start=started
        )
        _write_atomic(self.upgrading_path, record.model_dump_json(indent=2))

    def clear_upgrading(self) -> None:
        self.upgrading_path.unlink(missing_ok=True)

    def set_last_check(self, check: LastCheck) -> None:
        _write_atomic(self.last_check_path, check.model_dump_json(indent=2))

    def append_operation(self, operation: Operation) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = (operation.model_dump_json() + "\n").encode()
        # One write on an O_APPEND descriptor: a concurrent reader sees the whole line or none of it.
        fd = os.open(self.operations_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def save_snapshot(self, version: Version, freeze: str, branch: str | None = None) -> Path:
        """Store what leaving version must be able to undo: its dependency snapshot and the branch HEAD was on (None:
        detached). Replaces an older record of the same commit; returns the snapshot's path."""
        if not freeze.strip():
            raise ValueError(f"refusing to store an empty dependency snapshot for {version.label}")
        stale = self.snapshot(version)
        path = self.snapshots_dir / f"{version.commit}.txt"
        branch_path = path.with_suffix(".branch")
        if branch is None:
            branch_path.unlink(missing_ok=True)
        else:
            _write_atomic(branch_path, branch)
        _write_atomic(path, freeze)
        if stale is not None and stale != path:
            stale.unlink()
            stale.with_suffix(".branch").unlink(missing_ok=True)
        return path


def _process_start(pid: int) -> int | None:
    """When process pid started (clock ticks after boot, /proc/<pid>/stat field 22); None if there is no such process."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError, ProcessLookupError:
        return None
    # Field 2 (the command name) is in parentheses and may contain spaces: count fields after its closing one.
    return int(stat.rpartition(")")[2].split()[19])


def _read[M: BaseModel](path: Path, model: type[M]) -> M | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return model.model_validate_json(data)
    except ValidationError as exc:
        raise VersionStateError(path, str(exc)) from exc


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
