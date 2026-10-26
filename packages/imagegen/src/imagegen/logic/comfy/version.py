"""Which ComfyUI version is installed: read from the git checkout at ComfyConfig.root. Read-only."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_S = 10.0
# What `git describe --exact-match` says when HEAD simply has no tag (no tags at all / none on this commit).
_NO_TAG = ("no tag exactly matches", "no names found")


@dataclass(frozen=True, slots=True)
class InstalledVersion:
    tag: str | None  # the tag exactly at HEAD, e.g. "v0.42.5"; None when HEAD is not a tagged commit
    commit: str  # abbreviated HEAD commit, e.g. "0b05cd8"


class VersionReadError(RuntimeError):
    """git could not tell which version is checked out at root."""


def read_installed_version(root: Path) -> InstalledVersion:
    commit = _git(root, "rev-parse", "--short", "HEAD")
    if commit.returncode != 0:
        raise VersionReadError(_describe_failure(root, commit))
    described = _git(root, "describe", "--tags", "--exact-match")
    if described.returncode == 0:
        return InstalledVersion(described.stdout.strip(), commit.stdout.strip())
    if any(marker in described.stderr.lower() for marker in _NO_TAG):
        return InstalledVersion(None, commit.stdout.strip())
    raise VersionReadError(_describe_failure(root, described))


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(root), *args]
    try:
        # LC_ALL=C: the "no tag" messages are matched in English; git translates them otherwise.
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VersionReadError(f"cannot run {' '.join(command)}: {exc}") from exc


def _describe_failure(root: Path, result: subprocess.CompletedProcess[str]) -> str:
    command = " ".join(str(part) for part in result.args)
    return (
        f"cannot read the ComfyUI version at {root}: `{command}` exited {result.returncode}: "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )
