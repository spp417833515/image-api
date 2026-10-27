"""Operations on the ComfyUI installation at ComfyConfig.root: git (versions), uv (dependencies), systemd (service).

Nothing here decides when to do what (the upgrade flow does). Every failure raises InstallError carrying the command
and its original output. Commands run through a Runner, so tests can stand in for git / uv / systemctl.

torch, torchvision, torchaudio and torchsde are never reinstalled: the configured package index has no wheels for the
installed CUDA build, so every dependency change pins them to what is installed and verifies they are unchanged after.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .client import ComfyClient, ComfyHTTPError, ComfyUnreachable
from .config import ComfyConfig
from .version import InstalledVersion, read_installed_version

PINNED = ("torch", "torchvision", "torchaudio", "torchsde")
SERVICE = "comfyui.service"
RELEASE_PATTERN = "refs/tags/v[0-9]*"

_GIT_TIMEOUT_S = 60.0
_FETCH_TIMEOUT_S = 300.0
_UV_TIMEOUT_S = 1800.0  # downloads
_SYSTEMCTL_TIMEOUT_S = 150.0  # stop waits for ComfyUI to exit (systemd's own stop timeout is 90 s)
_POLL_S = 1.0
# The log kept with a failed start: the tail of the unit's current run, short enough to show on the page.
_JOURNAL_TAIL_LINES = 20
_JOURNAL_TAIL_BYTES = 2000
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")  # terminal colour / cursor codes


class InstallError(RuntimeError):
    """An install operation failed. raw is the original output (command stderr, journal, ComfyUI's answer)."""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(f"{message}\n{raw}" if raw else message)
        self.message = message
        self.raw = raw


@dataclass(frozen=True, slots=True)
class Release:
    tag: str  # e.g. "v0.42.5"
    commit: str  # abbreviated commit the tag points to


def _c_locale() -> Mapping[str, str]:
    """The environment for git: its messages in English, as read_installed_version also expects."""
    return {**os.environ, "LC_ALL": "C"}


type Runner = Callable[[Sequence[str], Path, Mapping[str, str], float], subprocess.CompletedProcess[str]]


def run_command(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv), cwd=cwd, env=dict(env), capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f"cannot run `{' '.join(argv)}`: {exc}") from exc


class ComfyInstall:
    def __init__(
        self,
        config: ComfyConfig,
        service: str = SERVICE,
        python: Path | None = None,
        run: Runner = run_command,
    ) -> None:
        """python: the interpreter of ComfyUI's virtual environment (default <root>/.venv/bin/python).

        Commands run with the installation as their working directory, so every path given to them is absolute."""
        self.config = config
        self.root = config.root.resolve()
        self.service = service
        self.python = (python or self.root / ".venv/bin/python").absolute()
        self._run = run

    # ---- versions (git)
    def installed(self) -> InstalledVersion:
        return read_installed_version(self.root)

    def fetch_releases(self) -> None:
        """Download the release tags from origin (the clone's fetch refspec covers master only)."""
        self._git(["fetch", "--tags", "origin"], _FETCH_TIMEOUT_S)

    def releases(self) -> list[Release]:
        """Release tags known locally, newest first."""
        peeled = "%(if)%(*objectname)%(then)%(*objectname:short)%(else)%(objectname:short)%(end)"
        listed = self._git(
            ["for-each-ref", "--sort=-v:refname", f"--format=%(refname:strip=2) {peeled}", RELEASE_PATTERN]
        )
        return [Release(*line.split()) for line in listed.splitlines() if line.strip()]

    def require_clean(self) -> None:
        changed = self._git(["status", "--porcelain"])
        if changed.strip():
            raise InstallError(f"ComfyUI 的工作区有改动（{self.root}），不切换版本；先处理这些文件", changed.rstrip())

    def branch(self) -> str | None:
        """The branch HEAD is on; None when HEAD is detached."""
        result = self._run(
            ["git", "-C", str(self.root), "symbolic-ref", "-q", "--short", "HEAD"],
            self.root,
            _c_locale(),
            _GIT_TIMEOUT_S,
        )
        if result.returncode == 1:  # git's answer for a detached HEAD
            return None
        if result.returncode != 0:
            raise InstallError(f"cannot read the branch of {self.root}", result.stderr.strip())
        return result.stdout.strip()

    def checkout(self, commit: str, branch: str | None = None) -> str | None:
        """Switch the working tree to commit. If branch is given and still points at commit, the branch is checked
        out (HEAD attached, as before the upgrade); otherwise HEAD is detached at commit. Returns the branch HEAD is
        on, None when detached. Refuses a dirty working tree."""
        self.require_clean()
        if branch is not None and self._branch_points_at(branch, commit):
            self._git(["checkout", branch])
        else:
            self._git(["checkout", "--detach", commit])
        installed = self.installed()
        if not _same_commit(installed.commit, commit):
            raise InstallError(f"git checkout {commit} left HEAD at {installed.commit}")
        return self.branch()

    # ---- dependencies (uv)
    def freeze(self) -> str:
        """Every installed package of ComfyUI's environment, `name==version` per line."""
        return self._checked(["uv", "pip", "freeze", "--python", str(self.python)], _UV_TIMEOUT_S, os.environ)

    def pinned(self) -> dict[str, str]:
        """The installed version of each PINNED package that is installed."""
        return _pinned_in(self.freeze())

    def sync_requirements(self) -> None:
        """Install ComfyUI's requirements.txt for the checked-out version, PINNED packages held where they are."""
        self._with_pins(["install", "-r", str(self.root / "requirements.txt")])

    def restore(self, snapshot: Path) -> None:
        """Make the environment exactly snapshot (a freeze() output), PINNED packages held where they are."""
        self._with_pins(["sync", str(snapshot.resolve())])

    # ---- service (systemd user unit)
    def service_state(self) -> str:
        """systemd's word for the unit: active / inactive / activating / failed / …"""
        result = self._run(["systemctl", "--user", "is-active", self.service], self.root, os.environ, 30.0)
        state = result.stdout.strip()
        if not state:
            raise InstallError(f"cannot read the state of {self.service}", result.stderr.strip())
        return state

    def stop(self) -> None:
        self._checked(["systemctl", "--user", "stop", self.service], _SYSTEMCTL_TIMEOUT_S, os.environ)
        if self.service_state() == "active":
            raise InstallError(f"{self.service} is still active after systemctl stop")

    def start(self) -> None:
        self._checked(["systemctl", "--user", "start", self.service], _SYSTEMCTL_TIMEOUT_S, os.environ)

    def journal(self) -> str:
        """The end of the log of the unit's current run (where ComfyUI's own start-up error is): only this run (its
        InvocationID, not earlier ones), colour codes removed, at most _JOURNAL_TAIL_LINES lines / _JOURNAL_TAIL_BYTES."""
        show = ["systemctl", "--user", "show", "-p", "InvocationID", "--value", self.service]
        invocation = self._checked(show, 30.0, os.environ).strip()
        if not invocation:
            return f"（{self.service} 还没有运行过，没有日志）"
        argv = ["journalctl", "--user", f"_SYSTEMD_INVOCATION_ID={invocation}", "-n", str(_JOURNAL_TAIL_LINES)]
        return _tail(_ANSI.sub("", self._checked([*argv, "--no-pager", "-o", "cat"], 30.0, os.environ)))

    # ---- the running ComfyUI (HTTP)
    async def is_online(self) -> bool:
        try:
            async with ComfyClient(self.config) as client:
                await client.system_stats()
        except ComfyUnreachable, ComfyHTTPError:
            return False
        return True

    async def wait_online(self, timeout_s: float) -> None:
        """Until GET /system_stats answers. Fails at once if the unit has failed, else after timeout_s, with the
        unit's log (ComfyUI's original start-up error)."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                async with ComfyClient(self.config) as client:
                    await client.system_stats()
                return
            except (ComfyUnreachable, ComfyHTTPError) as exc:
                last = exc
            state = await asyncio.to_thread(self.service_state)
            if state in ("failed", "inactive"):
                raise InstallError(f"{self.service} 没起来（{state}）：{last}", await asyncio.to_thread(self.journal))
            if time.monotonic() >= deadline:
                raise InstallError(
                    f"ComfyUI 在 {timeout_s:.0f} 秒内没有上线（{self.config.host}，{self.service} {state}）：{last}",
                    await asyncio.to_thread(self.journal),
                )
            await asyncio.sleep(_POLL_S)

    async def wait_idle(self, timeout_s: float) -> None:
        """Until ComfyUI has nothing running or queued. Fails if it cannot be asked, or after timeout_s."""
        deadline = time.monotonic() + timeout_s
        async with ComfyClient(self.config) as client:
            while True:
                try:
                    queue = await client.queue()
                    running, pending = len(queue["queue_running"]), len(queue["queue_pending"])
                except (ComfyUnreachable, ComfyHTTPError) as exc:
                    raise InstallError(f"读不到 ComfyUI 的队列（{self.config.host}）", str(exc)) from exc
                except (KeyError, TypeError) as exc:
                    raise InstallError("GET /queue has an unknown shape", str(queue)[:2000]) from exc
                if running == 0 and pending == 0:
                    return
                if time.monotonic() >= deadline:
                    raise InstallError(
                        f"ComfyUI 的队列在 {timeout_s:.0f} 秒内没有清空（还有 {running} 个在跑、{pending} 个在排队），"
                        "没有升级；等它跑完再试"
                    )
                await asyncio.sleep(_POLL_S)

    # ---- helpers
    def _with_pins(self, uv_args: list[str]) -> None:
        before = self.pinned()
        with tempfile.TemporaryDirectory(prefix="imagegen-pins-") as scratch:  # under TMPDIR, removed afterwards
            pins = Path(scratch) / "pins.txt"
            pins.write_text("".join(f"{name}=={version}\n" for name, version in before.items()), encoding="utf-8")
            argv = ["uv", "pip", *uv_args, "--python", str(self.python), "-c", str(pins)]
            self._checked(argv, _UV_TIMEOUT_S, os.environ)
        after = self.pinned()
        if after != before:
            raise InstallError(
                f"{', '.join(PINNED)} must not change, but `uv pip {uv_args[0]}` changed them",
                f"before: {before}\nafter:  {after}",
            )

    def _branch_points_at(self, branch: str, commit: str) -> bool:
        argv = ["git", "-C", str(self.root), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]
        result = self._run(argv, self.root, _c_locale(), _GIT_TIMEOUT_S)
        return result.returncode == 0 and _same_commit(result.stdout.strip(), commit)

    def _git(self, args: list[str], timeout_s: float = _GIT_TIMEOUT_S) -> str:
        return self._checked(["git", "-C", str(self.root), *args], timeout_s, _c_locale())

    def _checked(self, argv: list[str], timeout_s: float, env: Mapping[str, str]) -> str:
        result = self._run(argv, self.root, env, timeout_s)
        if result.returncode != 0:
            raise InstallError(
                f"`{' '.join(argv)}` exited {result.returncode}", (result.stderr.strip() or result.stdout.strip())
            )
        return result.stdout


def _tail(log: str) -> str:
    lines = log.rstrip("\n").splitlines()[-_JOURNAL_TAIL_LINES:]
    data = "\n".join(lines).encode()
    if len(data) <= _JOURNAL_TAIL_BYTES:
        return data.decode()
    return "…" + data[-_JOURNAL_TAIL_BYTES:].decode(errors="ignore")


def _pinned_in(freeze: str) -> dict[str, str]:
    found = {}
    for line in freeze.splitlines():
        name, separator, version = line.strip().partition("==")
        if separator and name.lower() in PINNED:
            found[name.lower()] = version
    return found


def _same_commit(a: str, b: str) -> bool:
    """Abbreviations of one commit can differ in length (git lengthens them as the repository grows)."""
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))
