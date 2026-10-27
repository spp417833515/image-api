"""imagegen.logic.comfy.install: git against real throwaway repositories (offline: origin is a local directory);
uv, systemctl and journalctl scripted through the Runner; ComfyUI's HTTP answers patched on ComfyClient."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from imagegen.logic.comfy.client import ComfyClient, ComfyUnreachable
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.comfy.install import ComfyInstall, InstallError, run_command

PINS_BEFORE = (
    "numpy==2.3.0\ntorch==2.14.1+cu130\ntorchaudio==2.11.0+cu130\nTorchSDE==0.2.6\ntorchvision==0.29.1+cu130\n"
)
PINS_FILE = "torch==2.14.1+cu130\ntorchaudio==2.11.0+cu130\ntorchsde==0.2.6\ntorchvision==0.29.1+cu130\n"


class Scripted:
    """The Runner: git runs for real; uv / systemctl / journalctl answer from `answers` (first word after the tool →
    a list of (returncode, stdout, stderr), consumed in order; the last one repeats)."""

    def __init__(self) -> None:
        self.answers: dict[str, list[tuple[int, str, str]]] = {}
        self.calls: list[list[str]] = []
        self.pin_files: list[tuple[Path, str]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if argv[0] == "git":
            return run_command(argv, cwd, env, timeout_s)
        self.calls.append(argv)
        if "-c" in argv:
            pins = Path(argv[argv.index("-c") + 1])
            self.pin_files.append((pins, pins.read_text()))
        key = "journalctl" if argv[0] == "journalctl" else argv[2]  # uv pip <key> … / systemctl --user <key> …
        queue = self.answers[key]
        code, out, err = queue.pop(0) if len(queue) > 1 else queue[0]
        return subprocess.CompletedProcess(argv, code, out, err)


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(cwd: Path, *args: str) -> str:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    return subprocess.run(["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path) -> dict[str, str]:
    """A repository with tags v0.9.0 (lightweight) and v0.10.0 (annotated) and an untagged master head."""
    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    commits = {}
    for name in ("first", "second", "head"):
        (repo / "requirements.txt").write_text(f"{name}\n")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", name)
        commits[name] = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v0.9.0", commits["first"])
    git(repo, "tag", "-a", "v0.10.0", "-m", "release", commits["second"])
    return commits


@pytest.fixture
def root(tmp_path, origin) -> Path:
    git(tmp_path, "clone", "-q", "--no-tags", str(tmp_path / "origin"), "comfy")
    return tmp_path / "comfy"


@pytest.fixture
def runner() -> Scripted:
    return Scripted()


@pytest.fixture
def install(root, runner) -> ComfyInstall:
    return ComfyInstall(ComfyConfig(root=root, base_url="http://127.0.0.1:9"), run=runner)


def test_releases_are_fetched_and_listed_newest_first(install, root, origin) -> None:
    assert install.releases() == []  # the clone's refspec brings no tags
    install.fetch_releases()
    releases = install.releases()
    assert [r.tag for r in releases] == ["v0.10.0", "v0.9.0"]  # version order, not text order
    assert origin["second"].startswith(releases[0].commit)  # the annotated tag's commit, not the tag object
    assert origin["first"].startswith(releases[1].commit)


def test_checkout_detaches_at_the_commit_and_refuses_a_dirty_tree(install, root, origin) -> None:
    install.fetch_releases()
    assert install.checkout(origin["first"][:10]) is None and install.branch() is None  # detached
    installed = install.installed()
    assert (installed.tag, origin["first"].startswith(installed.commit)) == ("v0.9.0", True)
    (root / "requirements.txt").write_text("edited\n")
    (root / "stray.txt").write_text("x")
    with pytest.raises(InstallError, match="工作区有改动") as caught:
        install.checkout(origin["head"])
    assert "requirements.txt" in caught.value.raw and "stray.txt" in caught.value.raw
    assert install.installed().tag == "v0.9.0"


def test_checkout_returns_to_a_branch_only_while_it_points_at_the_commit(install, root, origin) -> None:
    assert install.branch() == "master"
    install.checkout(origin["first"])
    assert install.checkout(origin["head"][:8], branch="master") == "master" == install.branch()
    git(root, "branch", "-f", "elsewhere", origin["second"])
    assert install.checkout(origin["head"], branch="elsewhere") is None  # moved: stays detached at the commit
    assert origin["head"].startswith(install.installed().commit)
    assert install.checkout(origin["head"], branch="gone") is None


def test_a_requirements_sync_pins_all_four_torch_packages_to_what_is_installed(
    install, runner, root, tmp_path, monkeypatch
) -> None:
    scratch = tmp_path / "system-tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))  # where TMPDIR points
    runner.answers = {"freeze": [(0, PINS_BEFORE, "")], "install": [(0, "", "")]}
    install.sync_requirements()
    freeze, sync, after = runner.calls
    assert sync[:5] == ["uv", "pip", "install", "-r", str(root / "requirements.txt")]
    assert sync[5:7] == ["--python", str(root / ".venv/bin/python")]
    [(pins, content)] = runner.pin_files
    assert content == PINS_FILE  # torchsde too, whatever case pip reports its name in
    assert pins.parent.parent == scratch and not pins.parent.exists()  # scratch under TMPDIR, removed afterwards
    assert install.pinned() == {
        "torch": "2.14.1+cu130",
        "torchaudio": "2.11.0+cu130",
        "torchsde": "0.2.6",
        "torchvision": "0.29.1+cu130",
    }


def test_a_changed_torch_fails_loudly_with_both_versions(install, runner, root) -> None:
    changed = PINS_BEFORE.replace("torch==2.14.1+cu130", "torch==2.9.0")
    runner.answers = {"freeze": [(0, PINS_BEFORE, ""), (0, changed, "")], "sync": [(0, "", "")]}
    with pytest.raises(InstallError, match="must not change") as caught:
        install.restore(root / "snapshot.txt")
    assert "'torch': '2.14.1+cu130'" in caught.value.raw and "'torch': '2.9.0'" in caught.value.raw
    assert runner.calls[1][:4] == ["uv", "pip", "sync", str(root / "snapshot.txt")]


def test_paths_reach_the_commands_absolute_whatever_the_working_directory(runner, root, monkeypatch) -> None:
    """The commands run inside the installation: a snapshot under a relative data directory must still be found."""
    monkeypatch.chdir(root.parent)
    install = ComfyInstall(ComfyConfig(root=Path(root.name)), run=runner)
    runner.answers = {"freeze": [(0, PINS_BEFORE, "")], "sync": [(0, "", "")]}
    install.restore(Path("data/comfy/deps/0b05cd8a.txt"))
    sync = runner.calls[1]
    assert sync[3] == str(root.parent / "data/comfy/deps/0b05cd8a.txt")
    assert sync[sync.index("--python") + 1] == str(root / ".venv/bin/python")
    assert install.branch() == "master"  # git -C an absolute root, not root/root


def test_a_failed_uv_run_carries_its_original_output(install, runner, root) -> None:
    runner.answers = {"freeze": [(0, PINS_BEFORE, "")], "install": [(1, "", "No solution found when resolving")]}
    with pytest.raises(InstallError, match="exited 1") as caught:
        install.sync_requirements()
    assert caught.value.raw == "No solution found when resolving"


def test_stop_makes_sure_the_service_is_down(install, runner) -> None:
    runner.answers = {"stop": [(0, "", "")], "is-active": [(0, "active\n", "")]}
    with pytest.raises(InstallError, match="still active"):
        install.stop()
    runner.answers["is-active"] = [(3, "inactive\n", "")]
    install.stop()


def test_the_journal_is_this_runs_short_tail_without_colour_codes(install, runner) -> None:
    lines = [f"\x1b[32m[INFO]\x1b[0m line {n}" for n in range(30)]
    runner.answers = {"show": [(0, "a3744fbf\n", "")], "journalctl": [(0, "\n".join(lines) + "\n", "")]}
    tail = install.journal()
    assert runner.calls[1][:5] == ["journalctl", "--user", "_SYSTEMD_INVOCATION_ID=a3744fbf", "-n", "20"]
    assert tail.splitlines() == [f"[INFO] line {n}" for n in range(10, 30)]
    runner.answers["journalctl"] = [(0, "x" * 5000 + "\nTraceback: the end\n", "")]
    tail = install.journal()
    assert tail.startswith("…") and tail.endswith("Traceback: the end") and len(tail.encode()) <= 2003
    runner.answers["show"] = [(0, "\n", "")]
    assert "没有日志" in install.journal()


async def test_waiting_online_stops_at_once_on_a_failed_service_with_its_log(install, runner, monkeypatch) -> None:
    async def unreachable(self) -> dict:
        raise ComfyUnreachable("http://127.0.0.1:9/system_stats", "connection refused")

    monkeypatch.setattr(ComfyClient, "system_stats", unreachable)
    runner.answers = {
        "is-active": [(3, "failed\n", "")],
        "show": [(0, "a3744fbf\n", "")],
        "journalctl": [(0, "ImportError: comfy_aimdo\n", "")],
    }
    with pytest.raises(InstallError, match="没起来（failed）") as caught:
        await install.wait_online(600)
    assert caught.value.raw == "ImportError: comfy_aimdo"
    runner.answers["is-active"] = [(3, "activating\n", "")]
    with pytest.raises(InstallError, match="0 秒内没有上线"):
        await install.wait_online(0)


async def test_waiting_for_the_queue(install, monkeypatch) -> None:
    queues = [{"queue_running": [[1, "p1"]], "queue_pending": [[2, "p2"]]}]

    async def queue(self) -> dict:
        return queues[0]

    monkeypatch.setattr(ComfyClient, "queue", queue)
    with pytest.raises(InstallError, match="还有 1 个在跑、1 个在排队"):
        await install.wait_idle(0)
    queues[0] = {"queue_running": [], "queue_pending": []}
    await install.wait_idle(0)
    queues[0] = {"unexpected": True}
    with pytest.raises(InstallError, match="unknown shape"):
        await install.wait_idle(0)
