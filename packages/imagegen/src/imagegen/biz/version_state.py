"""The ComfyUI version state files: the single owner of their location and format.

<data_dir>/comfy/
  upgrading.json     present only while an upgrade / rollback runs: {"target", "since"}
  last_check.json    the checks of the most recent upgrade / rollback: {"at", "version", "items": [...]}
  operations.jsonl   one Operation per line, oldest first

The upgrade flow (another process: `image-api comfy …`) writes them; the service reads them for /api/status and to
refuse generation while an upgrade runs. Writes replace whole files atomically, so a reader never sees half a file.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Version(BaseModel):
    """An installed ComfyUI version. Build one from imagegen.logic.comfy.version.InstalledVersion with
    Version.model_validate(installed, from_attributes=True)."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    tag: str | None
    commit: str = Field(min_length=1)


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


class VersionStateError(Exception):
    """A state file exists but cannot be read as its model: fail loudly rather than guess."""

    def __init__(self, path: Path, problem: str) -> None:
        super().__init__(f"version state file {path} is unreadable: {problem}")
        self.path = path


class VersionState:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.upgrading_path = directory / "upgrading.json"
        self.last_check_path = directory / "last_check.json"
        self.operations_path = directory / "operations.jsonl"

    @classmethod
    def in_data_dir(cls, data_dir: Path) -> VersionState:
        """The state directory of an image-api data directory (the one the service and the CLI share)."""
        return cls(data_dir / "comfy")

    # ---- read (status, generation guard, the upgrade flow)
    def upgrading(self) -> Upgrading | None:
        return _read(self.upgrading_path, Upgrading)

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

    # ---- write (only the upgrade / rollback flow)
    def set_upgrading(self, state: Upgrading) -> None:
        _write_atomic(self.upgrading_path, state.model_dump_json(indent=2))

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
