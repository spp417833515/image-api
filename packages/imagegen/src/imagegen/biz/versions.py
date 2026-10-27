"""Switching the installed ComfyUI between official releases: upgrade, rollback, recover. One operation at a time.

upgrade(tag):  lock → store the current version's dependency snapshot and branch → mark upgrading → wait for ComfyUI's
               queue to drain → stop → check out tag → sync its requirements (torch* held) → start → checks.
               Passed: record it. Failed (a step or a check): switch back to the original version, branch and
               dependency snapshot, re-check, and record "reverted" with what failed and its original text.
rollback():    the same, back to the version the current one was upgraded from, restoring what was stored for it.
recover():     after an operation was interrupted: back to the version it started from, then the checks.

While an operation runs, upgrading.json makes the service refuse generation (503 upgrading). It is written only after
the original version's snapshot is stored, so recover always has somewhere to go back to. It is cleared whenever the
operation ends with ComfyUI on a checked version or unchanged. If switching back itself fails it stays: once this
process exits it reads as interrupted, and the status points to `recover`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .version_checks import Check
from .version_overview import Overview, ReleaseSource, as_version, overview, rollback_target
from .version_state import (
    RECOVER_COMMAND,
    CheckItem,
    LastCheck,
    Operation,
    OperationKind,
    OperationResult,
    Upgrading,
    Version,
    VersionState,
)

QUEUE_DRAIN_TIMEOUT_S = 600.0


class Installation(ReleaseSource, Protocol):
    """What the flow needs from the installed backend (ComfyUI: imagegen.logic.comfy.install.ComfyInstall).
    Every failure raises."""

    def require_clean(self) -> None: ...
    def branch(self) -> str | None: ...  # None: HEAD detached
    def checkout(self, commit: str, branch: str | None = None) -> str | None: ...  # returns the branch HEAD is on
    def freeze(self) -> str: ...  # the dependency snapshot
    def sync_requirements(self) -> None: ...
    def restore(self, snapshot: Path) -> None: ...
    def stop(self) -> None: ...
    def start(self) -> None: ...
    async def is_online(self) -> bool: ...
    async def wait_idle(self, timeout_s: float) -> None: ...


class NotSwitched(Exception):
    """The operation did not change anything (refused, or failed before ComfyUI was stopped)."""


class RevertFailed(Exception):
    """Switching back (or recovering) failed: ComfyUI is on an unchecked version; upgrading.json stays."""


@dataclass(frozen=True, slots=True)
class Outcome:
    operation: Operation  # result passed, or reverted with the note
    check: LastCheck  # the checks of the version ComfyUI is on now


class VersionManager:
    def __init__(
        self,
        install: Installation,
        state: VersionState,
        checks: Sequence[Check],
        report: Callable[[str], None] = lambda _: None,
        queue_timeout_s: float = QUEUE_DRAIN_TIMEOUT_S,
    ) -> None:
        """checks: run in order on every version switched to (imagegen.biz.version_checks.default_checks).
        report: called with one line per stage, for people watching."""
        if not checks:
            raise ValueError("an upgrade needs at least one check")
        self._install = install
        self._state = state
        self._checks = tuple(checks)
        self._report = report
        self._queue_timeout_s = queue_timeout_s

    def overview(self, fetch: bool = True) -> Overview:
        return overview(self._install, self._state, fetch)

    # ---- operations
    async def upgrade(self, tag: str) -> Outcome:
        with self._state.lock():
            with _refusing("没有升级"):
                self._refuse_if_interrupted()
                self._report("拉取正式版列表（git fetch --tags origin）")
                self._install.fetch_releases()
                releases = [as_version(r) for r in self._install.releases()]
                target = next((r for r in releases if r.tag == tag), None)
                if target is None:
                    newest = "、".join(r.label for r in releases[:5])
                    raise NotSwitched(f"没有正式版 {tag!r}；最新的几个：{newest}")
                origin = as_version(self._install.installed())
                if origin.same_commit(target):
                    raise NotSwitched(f"ComfyUI 已经是 {target.label}（{origin.commit}）")
                self._install.require_clean()
                origin_snapshot = self._store_origin(origin)
            return await self._switch(OperationKind.UPGRADE, origin, origin_snapshot, target, target_snapshot=None)

    async def rollback(self) -> Outcome:
        with self._state.lock():
            with _refusing("没有回滚"):
                self._refuse_if_interrupted()
                origin = as_version(self._install.installed())
                target = rollback_target(self._state.operations(), origin)
                if target is None:
                    raise NotSwitched(f"没有可回滚的版本：{origin.label} 不是一次通过检查的升级的结果")
                target_snapshot = self._state.snapshot(target)
                if target_snapshot is None:
                    raise NotSwitched(f"找不到 {target.label} 的依赖清单（{self._state.snapshots_dir}）")
                self._install.require_clean()
                origin_snapshot = self._store_origin(origin)
            return await self._switch(OperationKind.ROLLBACK, origin, origin_snapshot, target, target_snapshot)

    async def recover(self) -> Outcome:
        """Return to the version an interrupted operation started from (recorded as a rollback to it)."""
        with self._state.lock():
            with _refusing("没有恢复"):
                interrupted = self._state.interrupted()
                if interrupted is None:
                    raise NotSwitched("没有中断的升级，不需要恢复")
                target = interrupted.origin
                if target is None:
                    raise NotSwitched(f"中断的升级（目标 {interrupted.target}）没有记下原版本，无法自动恢复")
                snapshot = self._state.snapshot(target)
                if snapshot is None:
                    raise NotSwitched(f"找不到 {target.label} 的依赖清单（{self._state.snapshots_dir}）")
                current = as_version(self._install.installed())
                self._install.require_clean()
                if await self._install.is_online():
                    self._report("等 ComfyUI 的队列清空")
                    await self._install.wait_idle(self._queue_timeout_s)
            self._state.set_upgrading(Upgrading(target=target.label, since=_now()), origin=target)
            try:
                self._apply(target, snapshot)
            except Exception as exc:
                raise RevertFailed(f"恢复到 {target.label} 失败：{exc}\n修好后再运行 {RECOVER_COMMAND}") from exc
            check = await self._check(target)
            self._state.set_last_check(check)
            if (failed := _failure(check)) is not None:
                raise RevertFailed(f"恢复到 {target.label} 后检查没过：{failed}\n修好后再运行 {RECOVER_COMMAND}")
            operation = Operation(
                at=_now(), kind=OperationKind.ROLLBACK, from_=current, to=target, result=OperationResult.PASSED
            )
            self._state.append_operation(operation)
            self._state.clear_upgrading()
            self._report(f"已恢复到 {target.label}，检查通过")
            return Outcome(operation, check)

    # ---- the flow
    def _store_origin(self, origin: Version) -> Path:
        """What switching back needs: origin's dependency snapshot and branch. Stored before upgrading.json exists."""
        self._report(f"记下 {origin.label} 的依赖清单")
        return self._state.save_snapshot(origin, self._install.freeze(), branch=self._install.branch())

    async def _switch(
        self,
        kind: OperationKind,
        origin: Version,
        origin_snapshot: Path,
        target: Version,
        target_snapshot: Path | None,
    ) -> Outcome:
        """Switch from origin to target (sync its requirements, or restore target_snapshot), check, revert if needed."""
        self._state.set_upgrading(Upgrading(target=target.label, since=_now()), origin=origin)
        with _refusing("没有切换", clear=self._state):
            self._report("等 ComfyUI 的队列清空")
            await self._install.wait_idle(self._queue_timeout_s)
        # From here on ComfyUI changes: whatever fails is answered by switching back to origin.
        try:
            self._apply(target, target_snapshot)
        except Exception as exc:
            failure = str(exc)
        else:
            check = await self._check(target)
            failure = _failure(check)
            if failure is None:
                self._state.set_last_check(check)
                operation = Operation(at=_now(), kind=kind, from_=origin, to=target, result=OperationResult.PASSED)
                self._state.append_operation(operation)
                self._state.clear_upgrading()
                self._report(f"{target.label} 检查通过")
                return Outcome(operation, check)
        return await self._revert(kind, origin, target, origin_snapshot, failure)

    async def _revert(
        self, kind: OperationKind, origin: Version, target: Version, origin_snapshot: Path, failure: str
    ) -> Outcome:
        self._report(f"{target.label} 没过：{failure}")
        self._report(f"退回 {origin.label}")
        try:
            self._apply(origin, origin_snapshot)
        except Exception as exc:
            raise RevertFailed(
                f"{target.label} 没过（{failure}），自动退回 {origin.label} 也失败了：{exc}\n"
                f"ComfyUI 停在未检查的状态：修好后运行 {RECOVER_COMMAND}"
            ) from exc
        check = await self._check(origin)
        self._state.set_last_check(check)
        recheck_failure = _failure(check)
        note = failure if recheck_failure is None else f"{failure}\n退回 {origin.label} 后复查也没过：{recheck_failure}"
        operation = Operation(at=_now(), kind=kind, from_=origin, to=target, result=OperationResult.REVERTED, note=note)
        self._state.append_operation(operation)
        if recheck_failure is not None:
            raise RevertFailed(
                f"已退回 {origin.label}，但复查没过：{recheck_failure}\nComfyUI 需要处理：修好后运行 {RECOVER_COMMAND}"
            )
        self._state.clear_upgrading()
        self._report(f"已退回 {origin.label}，复查通过")
        return Outcome(operation, check)

    def _apply(self, version: Version, snapshot: Path | None) -> None:
        """Stop, check out version (on the branch it was left on, if that branch is still there), sync its
        requirements (snapshot None) or restore snapshot, start. Raises naming the step that failed."""
        branch = self._state.branch_of(version)
        self._step("停止 ComfyUI", self._install.stop)
        on = self._step(f"切换到 {version.label}", lambda: self._install.checkout(version.commit, branch))
        if branch is not None:
            self._report(
                f"回到分支 {branch}"
                if on == branch
                else f"分支 {branch} 已不指向 {version.commit}：HEAD 停在 {version.commit}（分离），没有切回分支"
            )
        if snapshot is None:
            self._step("同步依赖（torch 不动）", self._install.sync_requirements)
        else:
            self._step("恢复依赖清单", lambda: self._install.restore(snapshot))
        self._step("启动 ComfyUI", self._install.start)

    def _step[T](self, name: str, action: Callable[[], T]) -> T:
        self._report(name)
        try:
            return action()
        except Exception as exc:
            raise _StepFailed(f"{name}失败：{exc}") from exc

    async def _check(self, version: Version) -> LastCheck:
        items: list[CheckItem] = []
        for check in self._checks:
            if items and not items[-1].ok:
                items.append(CheckItem(name=check.name, label=check.label, ok=False, note="前一项没过，没有运行"))
                continue
            self._report(f"检查：{check.label}")
            try:
                await check.run()
            except Exception as exc:
                note = str(exc) or type(exc).__name__
                items.append(CheckItem(name=check.name, label=check.label, ok=False, note=note))
                self._report(f"  没过：{note}")
            else:
                items.append(CheckItem(name=check.name, label=check.label, ok=True))
        return LastCheck(at=_now(), version=version, items=items)

    def _refuse_if_interrupted(self) -> None:
        if (interrupted := self._state.interrupted()) is not None:
            raise NotSwitched(
                f"上次切换到 {interrupted.target} 中断了（{interrupted.since.isoformat()} 开始），"
                f"先运行 {RECOVER_COMMAND}"
            )


class _StepFailed(Exception):
    pass


@contextmanager
def _refusing(what: str, clear: VersionState | None = None) -> Iterator[None]:
    """Any failure in the block means nothing was changed: it becomes NotSwitched, and upgrading.json is cleared if
    clear is given."""
    try:
        yield
    except BaseException as exc:
        if clear is not None:
            clear.clear_upgrading()
        if isinstance(exc, Exception) and not isinstance(exc, NotSwitched):
            raise NotSwitched(f"{what}：{exc}") from exc
        raise


def _failure(check: LastCheck) -> str | None:
    failed = next((item for item in check.items if not item.ok), None)
    return None if failed is None else f"{failed.label}：{failed.note}"


def _now() -> datetime:
    return datetime.now().astimezone()
