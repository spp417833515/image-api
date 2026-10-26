"""Reading the installed ComfyUI version (imagegen.logic.comfy.version) from a real local git repository."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from imagegen.logic.comfy.version import InstalledVersion, VersionReadError, read_installed_version


def git(root: Path, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env={**os.environ, **env})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_installed_version_from_git(tmp_path) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "one")
    commit = git(tmp_path, "rev-parse", "--short", "HEAD")
    assert read_installed_version(tmp_path) == InstalledVersion(None, commit)  # no tags at all
    git(tmp_path, "tag", "v0.1.0")
    assert read_installed_version(tmp_path) == InstalledVersion("v0.1.0", commit)
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "two")
    later = git(tmp_path, "rev-parse", "--short", "HEAD")
    assert read_installed_version(tmp_path) == InstalledVersion(None, later)  # tags exist, none on HEAD


def test_the_no_tag_message_is_recognised_whatever_the_users_locale(tmp_path, monkeypatch) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "one")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setenv("LANGUAGE", "zh_CN")
    assert read_installed_version(tmp_path).tag is None


def test_not_a_git_checkout_fails_loudly(tmp_path) -> None:
    with pytest.raises(VersionReadError, match="rev-parse"):
        read_installed_version(tmp_path / "missing")
