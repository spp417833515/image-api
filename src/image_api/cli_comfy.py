"""`image-api comfy …`: the ComfyUI installation itself — versions, upgrade, rollback, recover. The page only shows it.

Exit codes of upgrade / rollback / recover:
  0  the checks passed
  1  the checks failed and ComfyUI was switched back (the reason is printed)
  2  nothing was changed (refused, e.g. another upgrade runs, unknown version, ComfyUI busy)
  3  switching back or recovering failed: ComfyUI is on an unchecked version (the recover command is printed)
  4  an unexpected error (traceback printed, then the state ComfyUI is in and what to do); also `versions` failing
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any

from imagegen.biz.version_overview import Overview
from imagegen.biz.version_state import (
    RECOVER_COMMAND,
    LastCheck,
    Operation,
    OperationKind,
    OperationResult,
    UpgradeBusy,
    Version,
)
from imagegen.biz.versions import NotSwitched, Outcome, RevertFailed, VersionManager
from imagegen.factory import create_version_manager

from .config import Settings, data_dir_arg

EXIT_PASSED, EXIT_REVERTED, EXIT_NOT_SWITCHED, EXIT_NEEDS_RECOVER, EXIT_UNEXPECTED = 0, 1, 2, 3, 4
SHOWN_RELEASES = 10

type Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def register(commands: Subparsers) -> None:
    settings = Settings.from_env()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        type=data_dir_arg,
        default=settings.data_dir,
        help=f"数据目录，和 serve 用同一个（默认 {settings.data_dir}）",
    )
    comfy = commands.add_parser("comfy", help="ComfyUI 本体：看版本、升级、回滚")
    actions = comfy.add_subparsers(dest="comfy_command", required=True, metavar="ACTION")

    versions = actions.add_parser("versions", parents=[common], help="当前版本、正式版列表和各自的检查结果")
    versions.add_argument("--all", action="store_true", help=f"列出全部正式版（默认最新 {SHOWN_RELEASES} 个）")
    versions.add_argument("--no-fetch", action="store_true", help="不联网拉取正式版，只列本地已知的")
    versions.set_defaults(run=run_versions)

    upgrade = actions.add_parser("upgrade", parents=[common], help="升级到一个正式版：自动检查，没过自动退回")
    upgrade.add_argument("version", help="正式版标签，例如 v0.42.5")
    upgrade.set_defaults(run=lambda args: _operate(args, lambda manager: manager.upgrade(args.version)))

    rollback = actions.add_parser("rollback", parents=[common], help="回到升级前的版本（同样检查，没过自动退回）")
    rollback.set_defaults(run=lambda args: _operate(args, VersionManager.rollback))

    recover = actions.add_parser("recover", parents=[common], help="上次升级中断后：回到它开始前的版本并检查")
    recover.set_defaults(run=lambda args: _operate(args, VersionManager.recover))


def run_versions(args: argparse.Namespace) -> int:
    try:
        overview = create_version_manager(args.data_dir).overview(fetch=not args.no_fetch)
    except Exception as exc:
        hint = "" if args.no_fetch else "（--no-fetch 只看本地已知的版本）"
        print(f"读不到 ComfyUI 的版本{hint}：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED
    print("\n".join(_overview_lines(overview, show_all=args.all)))
    return 0


def _operate(args: argparse.Namespace, start: Callable[[VersionManager], Coroutine[Any, Any, Outcome]]) -> int:
    manager = create_version_manager(args.data_dir, report=_progress)
    try:
        outcome = asyncio.run(start(manager))
    except (NotSwitched, UpgradeBusy) as exc:
        print(f"ComfyUI 没有改动：{exc}", file=sys.stderr)
        return EXIT_NOT_SWITCHED
    except RevertFailed as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NEEDS_RECOVER
    except Exception:
        traceback.print_exc()
        print(f"意外错误，没有做完。{_state_after_error(manager)}", file=sys.stderr)
        return EXIT_UNEXPECTED
    operation = outcome.operation
    if operation.result is OperationResult.PASSED:
        print(f"通过：ComfyUI 现在是 {_name(operation.to)}")
        print("\n".join(_check_lines(outcome.check)))
        return EXIT_PASSED
    print(f"{_name(operation.to)} 没过，已退回 {_name(operation.from_)}（复查通过）", file=sys.stderr)
    print(f"没过的一项：{operation.note}", file=sys.stderr)
    return EXIT_REVERTED


def _state_after_error(manager: VersionManager) -> str:
    """Where an unexpected error left ComfyUI, and what to do about it."""
    try:
        overview = manager.overview(fetch=False)
    except Exception as exc:
        return f"读不到 ComfyUI 现在的状态（{type(exc).__name__}: {exc}）：用 image-api comfy versions 查看"
    now = f"ComfyUI 现在是 {_name(overview.current)}"
    if overview.upgrading is not None or overview.interrupted is not None:
        return f"{now}，没有检查完；升级标记还在，这个命令退出后显示为「中断」：运行 {RECOVER_COMMAND} 恢复"
    return f"{now}，没有留下升级标记（不需要 recover）"


def _progress(line: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {line}", flush=True)


def _overview_lines(overview: Overview, show_all: bool) -> list[str]:
    current = overview.current
    check = overview.current_check
    checked = "还没检查过" if check is None else f"{_check_word(check)}（{_time(check.at)}）"
    lines = [f"ComfyUI 当前版本：{_name(current)} · {checked}"]
    if check is not None:
        lines += _check_lines(check)
    if (upgrading := overview.upgrading) is not None:
        lines.append(f"正在切换到 {upgrading.target}（{_time(upgrading.since)} 开始）")
    if (interrupted := overview.interrupted) is not None:
        origin = "未知" if interrupted.origin is None else _name(interrupted.origin)
        lines.append(
            f"上次切换到 {interrupted.target} 中断了（{_time(interrupted.since)} 开始，原版本 {origin}）："
            f"运行 {RECOVER_COMMAND} 恢复"
        )
    if (operation := overview.last_operation) is not None:
        lines.append(f"最近一次操作：{_operation_line(operation)}")
    if overview.rollback_to is not None:
        lines.append(f"可以回滚到：{_name(overview.rollback_to)}（image-api comfy rollback）")
    shown = overview.releases if show_all else overview.releases[:SHOWN_RELEASES]
    lines.append(f"正式版（新的在前，共 {len(overview.releases)} 个）：")
    for release in shown:
        mark = "  ← 当前" if release.version.same_commit(current) else ""
        lines.append(f"  {release.version.tag:<10} {release.version.commit:<10} {_release_word(release.last)}{mark}")
    if hidden := len(overview.releases) - len(shown):
        lines.append(f"  …还有 {hidden} 个更早的（--all 全部列出）")
    return lines


def _check_lines(check: LastCheck) -> list[str]:
    return [f"  {'✓' if item.ok else '✗'} {item.label}" + ("" if item.ok else f"：{item.note}") for item in check.items]


def _check_word(check: LastCheck) -> str:
    return "检查通过" if all(item.ok for item in check.items) else "检查没过"


def _release_word(last: Operation | None) -> str:
    if last is None:
        return "没检查过"
    if last.result is OperationResult.PASSED:
        return f"检查通过（{_time(last.at)}）"
    return f"检查没过，已退回（{_time(last.at)}）"


def _operation_line(operation: Operation) -> str:
    kind = "升级" if operation.kind is OperationKind.UPGRADE else "回滚"
    result = "检查通过" if operation.result is OperationResult.PASSED else f"检查没过，已退回：{operation.note}"
    return f"{_time(operation.at)} {kind} {_name(operation.from_)} → {_name(operation.to)}，{result}"


def _name(version: Version) -> str:
    return f"{version.tag}（{version.commit}）" if version.tag else f"{version.commit}（没有标签）"


def _time(at: datetime) -> str:
    return f"{at.astimezone():%Y-%m-%d %H:%M}"
