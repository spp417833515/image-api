"""The upgrade / rollback / recover flow (imagegen.biz.versions) over a scripted installation and scripted checks."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import UTC, datetime

import pytest
from imagegen.biz.version_checks import CheckFailed
from imagegen.biz.version_state import (
    RECOVER_COMMAND,
    Operation,
    OperationKind,
    OperationResult,
    UpgradeBusy,
    Version,
    VersionState,
)
from imagegen.biz.versions import NotSwitched, RevertFailed, rollback_target
from versions_fakes import (
    BASE,
    V424,
    V425,
    ProcessKilled,
    WatchedState,
    kill_recorder,
    manager,
    scripted_checks,
)

ORIGIN = Version(tag=None, commit=BASE.commit)
TARGET = Version(tag=V425.tag, commit=V425.commit)
SWITCH_TO_TARGET = ["stop", f"checkout {V425.commit}", "sync", "start"]
SWITCH_BACK = ["stop", f"checkout {BASE.commit}", f"restore comfyui=={BASE.commit}", "start"]


async def test_upgrade_runs_its_steps_in_order_and_records_the_pass(tmp_path) -> None:
    flow, install, state, reported = manager(tmp_path)
    outcome = await flow.upgrade("v0.42.5")
    assert install.calls == ["fetch", "require_clean", "freeze", "wait_idle", *SWITCH_TO_TARGET]
    assert install.upgrading_at_stop[0].target == "v0.42.5"  # generation was refused before ComfyUI stopped
    assert install.on_branch is None  # a release tag: HEAD detached
    assert (state.upgrading(), state.interrupted()) == (None, None)
    assert outcome.operation.result is OperationResult.PASSED and outcome.operation.kind is OperationKind.UPGRADE
    assert (outcome.operation.from_, outcome.operation.to, outcome.operation.note) == (ORIGIN, TARGET, None)
    assert state.last_operation() == outcome.operation
    assert state.last_check() == outcome.check and outcome.check.version == TARGET
    assert [(i.name, i.ok) for i in outcome.check.items] == [("starts", True), ("nodes", True), ("routes", True)]
    assert state.snapshot(ORIGIN).read_text().startswith(f"comfyui=={BASE.commit}")
    assert state.branch_of(ORIGIN) == "master"
    assert state.snapshot(TARGET) is None  # stored when the flow leaves it, not before
    assert "停止 ComfyUI" in reported and "检查：三条路试跑" in reported


async def test_a_failed_check_switches_back_and_records_which_check_with_its_text(tmp_path) -> None:
    checks = scripted_checks(routes={0: CheckFailed("文生图：Value 0 smaller than min of 1: steps")})
    flow, install, state, _ = manager(tmp_path, checks)
    outcome = await flow.upgrade("v0.42.5")
    assert install.calls[4:] == [*SWITCH_TO_TARGET, *SWITCH_BACK]
    assert install.head == BASE
    assert [c.runs for c in checks] == [2, 2, 2]  # the target's checks, then the re-check of the original
    operation = outcome.operation
    assert operation.result is OperationResult.REVERTED and (operation.from_, operation.to) == (ORIGIN, TARGET)
    assert operation.note == "三条路试跑：文生图：Value 0 smaller than min of 1: steps"
    assert state.last_check() == outcome.check and outcome.check.version == ORIGIN
    assert all(item.ok for item in outcome.check.items)
    assert state.upgrading() is None and state.interrupted() is None


async def test_checks_after_a_failed_one_do_not_run(tmp_path) -> None:
    checks = scripted_checks(starts={0: RuntimeError("comfyui.service 没起来（failed）")})
    flow, _, state, _ = manager(tmp_path, checks)
    outcome = await flow.upgrade("v0.42.5")
    assert [c.runs for c in checks] == [2, 1, 1]
    assert outcome.operation.note == "能启动：comfyui.service 没起来（failed）"
    assert all(item.ok for item in outcome.check.items)  # the re-check of the original


async def test_a_failed_check_is_reported_as_it_happens(tmp_path) -> None:
    checks = scripted_checks(nodes={0: CheckFailed("蒙版改写：缺少节点 ZImageFunControlnet")})
    flow, _, _, reported = manager(tmp_path, checks)
    await flow.upgrade("v0.42.5")
    assert "  没过：蒙版改写：缺少节点 ZImageFunControlnet" in reported
    assert checks[2].runs == 1  # routes ran only in the re-check


async def test_a_failed_step_switches_back_naming_the_step(tmp_path) -> None:
    flow, install, state, _ = manager(tmp_path)
    install.failures["sync"] = RuntimeError("`uv pip install` exited 1\nNo solution found when resolving")
    outcome = await flow.upgrade("v0.42.5")
    assert install.calls[4:] == ["stop", f"checkout {V425.commit}", "sync", *SWITCH_BACK]
    assert outcome.operation.result is OperationResult.REVERTED
    assert outcome.operation.note.startswith("同步依赖（torch 不动）失败：`uv pip install` exited 1")
    assert "No solution found" in outcome.operation.note
    assert state.upgrading() is None


async def test_a_failed_switch_back_keeps_upgrading_and_recover_repairs_it(tmp_path) -> None:
    checks = scripted_checks(routes={0: CheckFailed("文生图：boom")})
    flow, install, state, _ = manager(tmp_path, checks)
    install.failures[f"checkout {BASE.commit}"] = RuntimeError("error: unable to unlink old 'nodes.py'")
    with pytest.raises(RevertFailed) as caught:
        await flow.upgrade("v0.42.5")
    assert RECOVER_COMMAND in str(caught.value) and "unable to unlink" in str(caught.value)
    assert state.last_operation() is None  # nothing finished
    assert state.upgrading() is not None  # still this process: generation stays refused
    kill_recorder(state)  # … and once this process is gone it reads as interrupted, pointing back to the origin
    interrupted = state.interrupted()
    assert interrupted is not None and interrupted.origin == ORIGIN and interrupted.target == "v0.42.5"

    with pytest.raises(NotSwitched, match="recover"):
        await flow.upgrade("v0.42.4")
    install.calls.clear()
    outcome = await flow.recover()
    assert install.calls == ["require_clean", "wait_idle", *SWITCH_BACK]
    assert outcome.operation.kind is OperationKind.ROLLBACK and outcome.operation.result is OperationResult.PASSED
    assert (outcome.operation.from_, outcome.operation.to) == (TARGET, ORIGIN)
    assert state.upgrading_path.exists() is False and state.interrupted() is None


async def test_a_failed_recheck_records_the_revert_and_keeps_upgrading(tmp_path) -> None:
    checks = scripted_checks(routes={0: CheckFailed("文生图：a"), 1: CheckFailed("文生图：b")})
    flow, install, state, _ = manager(tmp_path, checks)
    with pytest.raises(RevertFailed, match="复查没过"):
        await flow.upgrade("v0.42.5")
    assert install.head == BASE
    operation = state.last_operation()
    assert operation.result is OperationResult.REVERTED
    assert operation.note == f"三条路试跑：文生图：a\n退回 {BASE.commit} 后复查也没过：三条路试跑：文生图：b"
    assert not state.last_check().items[2].ok
    assert state.upgrading_path.exists()


async def test_a_queue_that_does_not_drain_changes_nothing(tmp_path) -> None:
    flow, install, state, _ = manager(tmp_path)
    install.failures["wait_idle"] = RuntimeError("ComfyUI 的队列在 600 秒内没有清空（还有 1 个在跑、0 个在排队）")
    with pytest.raises(NotSwitched, match="队列在 600 秒内没有清空"):
        await flow.upgrade("v0.42.5")
    assert "stop" not in install.calls
    assert not state.upgrading_path.exists() and state.last_operation() is None and state.last_check() is None


@pytest.mark.parametrize(
    ("tag", "head", "dirty", "says"),
    [
        ("v9.9.9", BASE, False, "没有正式版 'v9.9.9'；最新的几个：v0.42.5、v0.42.4"),
        ("v0.42.5", V425, False, "已经是 v0.42.5"),
        ("v0.42.5", BASE, True, "工作区有改动"),
    ],
)
async def test_refused_upgrades_change_nothing(tmp_path, tag, head, dirty, says) -> None:
    flow, install, state, _ = manager(tmp_path, head=head)
    install.dirty = dirty
    with pytest.raises(NotSwitched, match=says):
        await flow.upgrade(tag)
    assert "stop" not in install.calls and not state.upgrading_path.exists()


async def test_rollback_goes_back_to_the_version_upgraded_from_with_its_snapshot_and_branch(tmp_path) -> None:
    flow, install, state, reported = manager(tmp_path)
    await flow.upgrade("v0.42.5")
    install.calls.clear()
    outcome = await flow.rollback()
    assert install.calls == ["require_clean", "freeze", "wait_idle", *SWITCH_BACK]
    assert outcome.operation.kind is OperationKind.ROLLBACK and outcome.operation.result is OperationResult.PASSED
    assert (outcome.operation.from_, outcome.operation.to) == (TARGET, ORIGIN)
    assert install.on_branch == "master" and "回到分支 master" in reported
    with pytest.raises(NotSwitched, match="没有可回滚的版本"):
        await flow.rollback()


async def test_rollback_stays_detached_when_the_branch_has_moved_and_says_so(tmp_path) -> None:
    flow, install, _, reported = manager(tmp_path)
    await flow.upgrade("v0.42.5")
    install.branches["master"] = V424.commit  # someone moved master meanwhile
    await flow.rollback()
    assert install.head == BASE and install.on_branch is None
    assert f"分支 master 已不指向 {BASE.commit}：HEAD 停在 {BASE.commit}（分离），没有切回分支" in reported


@pytest.mark.parametrize("operation", ["upgrade", "rollback"])
async def test_the_origin_is_stored_before_upgrading_is_marked(tmp_path, operation) -> None:
    state = WatchedState(tmp_path / "comfy")
    flow, _, _, _ = manager(tmp_path, state=state)
    await flow.upgrade("v0.42.5")
    if operation == "rollback":
        await flow.rollback()
    assert state.snapshot_stored_when_marked == [True] * (1 if operation == "upgrade" else 2)


async def test_dying_while_the_origin_is_stored_leaves_nothing_to_recover(tmp_path) -> None:
    flow, install, state, _ = manager(tmp_path)
    install.failures["freeze"] = ProcessKilled()
    with pytest.raises(ProcessKilled):
        await flow.upgrade("v0.42.5")
    assert state.generation_block() is None and "stop" not in install.calls  # generation goes on, ComfyUI untouched
    assert (await flow.upgrade("v0.42.5")).operation.result is OperationResult.PASSED


@pytest.mark.parametrize("operation", ["upgrade", "rollback"])
async def test_dying_right_after_marking_upgrading_is_recoverable(tmp_path, operation) -> None:
    if operation == "rollback":
        await manager(tmp_path)[0].upgrade("v0.42.5")
    origin, head = (ORIGIN, BASE) if operation == "upgrade" else (TARGET, V425)
    dying = WatchedState(tmp_path / "comfy", die_after_marking=True)
    flow, install, _, _ = manager(tmp_path, state=dying, head=head)
    started = flow.rollback() if operation == "rollback" else flow.upgrade("v0.42.5")
    with pytest.raises(ProcessKilled):
        await started
    assert "stop" not in install.calls
    state = VersionState.in_data_dir(tmp_path)
    kill_recorder(state)  # the process is gone: generation is refused, pointing to recover
    assert state.interrupted().origin == origin
    with pytest.raises(NotSwitched, match="recover"):
        await manager(tmp_path, state=state, install=install)[0].upgrade("v0.42.4")

    recovering, install, state, _ = manager(tmp_path, state=state, install=install)
    install.calls.clear()
    outcome = await recovering.recover()
    restored = f"restore comfyui=={origin.commit}"
    assert install.calls == ["require_clean", "wait_idle", "stop", f"checkout {origin.commit}", restored, "start"]
    assert outcome.operation.result is OperationResult.PASSED and outcome.operation.to == origin
    assert state.generation_block() is None


async def test_rollback_without_a_snapshot_is_refused(tmp_path) -> None:
    flow, install, state, _ = manager(tmp_path)
    await flow.upgrade("v0.42.5")
    state.snapshot(ORIGIN).unlink()
    with pytest.raises(NotSwitched, match="找不到 0b05cd8a 的依赖清单"):
        await flow.rollback()


def operation(kind: OperationKind, result: OperationResult, from_: Version, to: Version) -> Operation:
    return Operation(at=datetime(2026, 10, 26, tzinfo=UTC), kind=kind, from_=from_, to=to, result=result)


def test_rollback_target_walks_back_one_passed_upgrade_at_a_time() -> None:
    up, down = OperationKind.UPGRADE, OperationKind.ROLLBACK
    passed, reverted = OperationResult.PASSED, OperationResult.REVERTED
    old = Version(tag=None, commit="0b05cd8")  # recorded before git lengthened its abbreviations
    a, b = Version(tag="v0.42.4", commit="3f438ca6"), Version(tag="v0.42.5", commit="483dae43")
    history = [operation(up, passed, old, a), operation(up, reverted, a, b), operation(up, passed, a, b)]
    assert rollback_target(history, b) == a
    history.append(operation(down, passed, b, a))
    assert rollback_target(history, a) == old
    assert rollback_target(history, Version(tag=None, commit="0b05cd8a")) is None
    assert rollback_target([], a) is None


async def test_an_interrupted_recover_recovers_again_to_the_last_checked_version(tmp_path) -> None:
    flow, install, state, _ = manager(tmp_path)
    install.failures["sync"] = ProcessKilled()  # the upgrade dies mid-switch: ComfyUI stopped on the target
    with pytest.raises(ProcessKilled):
        await flow.upgrade("v0.42.5")
    kill_recorder(state)
    install.failures[f"restore comfyui=={BASE.commit}"] = ProcessKilled()  # the recover dies mid-switch too
    with pytest.raises(ProcessKilled):
        await flow.recover()
    kill_recorder(state)
    interrupted = state.interrupted()
    assert interrupted.origin == ORIGIN and interrupted.target == ORIGIN.label  # still the last checked version

    install.calls.clear()
    outcome = await flow.recover()
    restored = f"restore comfyui=={BASE.commit}"
    assert install.calls == ["require_clean", "wait_idle", "stop", f"checkout {BASE.commit}", restored, "start"]
    assert outcome.operation.to == ORIGIN and outcome.operation.result is OperationResult.PASSED
    assert install.head == BASE and install.on_branch == "master" and state.generation_block() is None


async def test_recover_needs_an_interrupted_operation(tmp_path) -> None:
    flow, _, _, _ = manager(tmp_path)
    with pytest.raises(NotSwitched, match="没有中断的升级"):
        await flow.recover()


async def test_one_operation_at_a_time_in_this_process(tmp_path) -> None:
    flow, install, _, _ = manager(tmp_path)
    with VersionState.in_data_dir(tmp_path).lock():
        with pytest.raises(UpgradeBusy):
            await flow.upgrade("v0.42.5")
        with pytest.raises(UpgradeBusy):
            await flow.rollback()
    assert install.calls == []
    await flow.upgrade("v0.42.5")  # released


async def test_one_operation_at_a_time_across_processes(tmp_path) -> None:
    holder = textwrap.dedent(f"""
        import sys
        from pathlib import Path
        from imagegen.biz.version_state import VersionState
        with VersionState.in_data_dir(Path({str(tmp_path)!r})).lock():
            print("held", flush=True)
            sys.stdin.readline()
    """)
    process = subprocess.Popen([sys.executable, "-c", holder], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "held"
        flow, install, _, _ = manager(tmp_path)
        with pytest.raises(UpgradeBusy, match="另一个 ComfyUI 升级"):
            await flow.upgrade("v0.42.5")
        assert install.calls == []
    finally:
        process.kill()  # killed, not released: the kernel lets go of the lock with the process
        process.wait()
    await flow.upgrade("v0.42.5")


async def test_overview_shows_versions_and_what_is_known_about_each(tmp_path) -> None:
    flow, install, _, _ = manager(tmp_path)
    before = flow.overview(fetch=False)
    assert install.calls == [] and before.current == ORIGIN and before.current_check is None
    assert [(r.version, r.last) for r in before.releases] == [
        (TARGET, None),
        (Version.model_validate(V424, from_attributes=True), None),
    ]
    await flow.upgrade("v0.42.5")
    after = flow.overview()
    assert install.calls[-1] == "fetch"
    assert after.current == TARGET and after.rollback_to == ORIGIN
    assert after.current_check is not None and after.current_check.version == TARGET
    assert after.releases[0].last.result is OperationResult.PASSED and after.releases[1].last is None
    assert after.last_operation == after.releases[0].last
