"""What is known about the installed ComfyUI's versions (for `image-api comfy versions`), and which version a rollback
returns to. Read only: changes go through imagegen.biz.versions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .version_state import (
    Interrupted,
    LastCheck,
    Operation,
    OperationKind,
    OperationResult,
    Upgrading,
    Version,
    VersionState,
)


class ReleaseSource(Protocol):
    """Where versions come from. Versions are objects with .tag (str | None) and .commit (str)."""

    def installed(self) -> object: ...
    def fetch_releases(self) -> None: ...
    def releases(self) -> Sequence[object]: ...  # release tags, newest first


@dataclass(frozen=True, slots=True)
class ReleaseState:
    version: Version
    last: Operation | None  # the newest operation that switched to it (its result says whether its checks passed)


@dataclass(frozen=True, slots=True)
class Overview:
    current: Version
    current_check: LastCheck | None  # the last check, if it was of the current version
    releases: list[ReleaseState]  # newest first
    upgrading: Upgrading | None
    interrupted: Interrupted | None
    last_operation: Operation | None
    rollback_to: Version | None


def overview(source: ReleaseSource, state: VersionState, fetch: bool) -> Overview:
    """Current version, release tags (fetched from origin first when fetch) and what is known about each."""
    if fetch:
        source.fetch_releases()
    current = as_version(source.installed())
    operations = state.operations()
    last_check = state.last_check()

    def last_switch_to(version: Version) -> Operation | None:
        return next((op for op in reversed(operations) if op.to.same_commit(version)), None)

    return Overview(
        current=current,
        current_check=last_check if last_check and last_check.version.same_commit(current) else None,
        releases=[ReleaseState(r, last_switch_to(r)) for r in map(as_version, source.releases())],
        upgrading=state.upgrading(),
        interrupted=state.interrupted(),
        last_operation=operations[-1] if operations else None,
        rollback_to=rollback_target(operations, current),
    )


def rollback_target(operations: Sequence[Operation], current: Version) -> Version | None:
    """The version current was upgraded from: the newest passed upgrade that ended on current. Rolling back again
    then goes one upgrade further back."""
    for operation in reversed(operations):
        if (
            operation.kind is OperationKind.UPGRADE
            and operation.result is OperationResult.PASSED
            and operation.to.same_commit(current)
        ):
            return operation.from_
    return None


def as_version(installed: object) -> Version:
    return Version.model_validate(installed, from_attributes=True)
