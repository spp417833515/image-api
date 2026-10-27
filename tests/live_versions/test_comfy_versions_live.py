"""Real upgrade / rollback / automatic revert of the local ComfyUI (stops it for a few minutes each). No mocks.

The commands run as the user runs them (`image-api comfy …` as a subprocess) next to a real `image-api serve`
sharing the data directory, so /api/status and /api/generate are seen from outside as the page sees them. The one
test-only part is the fault injection: an in-process flow whose routes check fails on its first run (ComfyUI itself
rejects the trial graph, steps=0) and runs normally afterwards.

Starting state (recorded first, restored last, whatever happened in between): the checked-out commit and branch,
the full dependency list, comfyui.service active. torch* must be identical at the end.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from imagegen.biz.version_checks import NodesCheck, RoutesCheck, StartsCheck
from imagegen.biz.version_state import OperationResult, VersionState
from imagegen.biz.versions import VersionManager
from imagegen.logic.comfy.backend import ComfyBackend
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.comfy.install import ComfyInstall

pytestmark = pytest.mark.live_versions

TARGET = "v0.42.5"
CONFIG = ComfyConfig()
IMAGE_API = Path(sys.executable).with_name("image-api")
OPERATION_TIMEOUT_S = 1200
ONE_STEP = {"steps": [{"instruction": "A red fox sitting in fresh snow."}]}


@dataclass(frozen=True)
class Baseline:
    commit: str  # full sha
    branch: str | None  # e.g. "master"; None when HEAD was detached
    freeze: str
    pinned: dict[str, str]


def git(*args: str) -> str:
    done = subprocess.run(["git", "-C", str(CONFIG.root), *args], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def say(line: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)


@pytest.fixture(scope="module", autouse=True)
def baseline(tmp_path_factory) -> Iterator[Baseline]:
    install = ComfyInstall(CONFIG)
    assert git("status", "--porcelain") == "", "ComfyUI has local changes: not touching it"
    assert install.service_state() == "active"
    start = Baseline(git("rev-parse", "HEAD"), install.branch(), install.freeze(), install.pinned())
    assert set(start.pinned) >= {"torch", "torchvision", "torchaudio"}, start.pinned
    say(f"baseline {start.commit[:8]} on {start.branch or 'detached HEAD'}, torch* {start.pinned}")
    yield start
    restore(install, start, tmp_path_factory.mktemp("restore"))


def restore(install: ComfyInstall, start: Baseline, work: Path) -> None:
    """Back to the starting commit, branch and dependencies, service running; then assert it."""
    if git("rev-parse", "HEAD") != start.commit or install.freeze() != start.freeze:
        say("restoring the starting state")
        install.stop()
        install.checkout(start.commit, start.branch)
        snapshot = work / "baseline-freeze.txt"
        snapshot.write_text(start.freeze)
        install.restore(snapshot)
        install.start()
    if start.branch is not None and install.branch() != start.branch:
        install.checkout(start.commit, start.branch)  # the same commit: no file changes
    if install.service_state() != "active":
        install.start()
    asyncio.run(install.wait_online(180))
    assert git("rev-parse", "HEAD") == start.commit
    assert install.freeze() == start.freeze
    assert install.pinned() == start.pinned


@pytest.fixture(scope="module", autouse=True)
def leaves_no_files_behind() -> Iterator[None]:
    def ours() -> set[Path]:
        return {p for d in (CONFIG.input_dir, CONFIG.output_dir) for p in d.glob(f"{CONFIG.file_prefix}*")}

    before = ours()
    yield
    assert ours() - before == set()


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("data")


@pytest.fixture(scope="module")
def api(data_dir) -> Iterator[httpx.Client]:
    """The real `image-api serve` on a free port, sharing data_dir with the commands."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = (data_dir / "serve.log").open("w")
    server = subprocess.Popen(
        [IMAGE_API, "serve", "--port", str(port), "--data-dir", str(data_dir)], stdout=log, stderr=subprocess.STDOUT
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)
    deadline = time.monotonic() + 60
    while True:
        assert server.poll() is None, (data_dir / "serve.log").read_text()
        try:
            if client.get("/api/status").status_code == 200:
                break
        except httpx.TransportError:
            pass
        assert time.monotonic() < deadline, "image-api serve did not answer"
        time.sleep(0.2)
    yield client
    client.close()
    server.terminate()
    server.wait(timeout=30)
    log.close()


def comfy_command(data_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    done = subprocess.run(
        [IMAGE_API, "comfy", *args, "--data-dir", str(data_dir)],
        capture_output=True,
        text=True,
        timeout=OPERATION_TIMEOUT_S,
    )
    say(f"image-api comfy {' '.join(args)} → exit {done.returncode} in {time.monotonic() - started:.0f} s")
    print(done.stdout, done.stderr, sep="\n", flush=True)
    return done


@dataclass(frozen=True)
class UpgradeRun:
    exit_code: int
    output: str
    status_during: dict
    generate_during: httpx.Response


@pytest.fixture(scope="module")
def upgrade_run(data_dir, api) -> UpgradeRun:
    """`image-api comfy upgrade v0.42.5`, with /api/status and /api/generate asked while it runs."""
    started = time.monotonic()
    process = subprocess.Popen(
        [IMAGE_API, "comfy", "upgrade", TARGET, "--data-dir", str(data_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 120
    while (status := api.get("/api/status").json())["upgrading"] is None:
        assert process.poll() is None and time.monotonic() < deadline, "the upgrade never showed in /api/status"
        time.sleep(0.2)
    generate = api.post("/api/generate", json=ONE_STEP)
    output, _ = process.communicate(timeout=OPERATION_TIMEOUT_S)
    say(f"image-api comfy upgrade {TARGET} → exit {process.returncode} in {time.monotonic() - started:.0f} s")
    print(output, flush=True)
    return UpgradeRun(process.returncode, output, status, generate)


def assert_checked(status: dict, commit_prefix: str | None = None, tag: str | None = None) -> None:
    assert status["online"] and status["upgrading"] is None
    assert all(route["available"] for route in status["routes"].values()), status["routes"]
    check = status["last_check"]
    assert [(i["name"], i["ok"]) for i in check["items"]] == [("starts", True), ("nodes", True), ("routes", True)]
    assert check["version"] == status["version"]
    if tag is not None:
        assert status["version"]["tag"] == tag
    if commit_prefix is not None:
        assert commit_prefix.startswith(status["version"]["commit"])


def test_upgrade_to_the_release_passes_its_checks(upgrade_run, data_dir, api, baseline) -> None:
    assert upgrade_run.exit_code == 0, upgrade_run.output
    assert f"通过：ComfyUI 现在是 {TARGET}" in upgrade_run.output
    versions = comfy_command(data_dir, "versions", "--no-fetch")
    assert versions.returncode == 0
    assert (
        versions.stdout.startswith(f"ComfyUI 当前版本：{TARGET}（") and "· 检查通过" in versions.stdout.splitlines()[0]
    )
    status = api.get("/api/status").json()
    assert_checked(status, tag=TARGET)
    operation = status["last_operation"]
    assert (operation["kind"], operation["result"], operation["to"]["tag"]) == ("upgrade", "passed", TARGET)
    assert baseline.commit.startswith(operation["from"]["commit"])
    assert ComfyInstall(CONFIG).pinned() == baseline.pinned


def test_generation_is_503_upgrading_while_the_upgrade_runs(upgrade_run) -> None:
    assert upgrade_run.status_during["upgrading"]["target"] == TARGET
    assert upgrade_run.generate_during.status_code == 503
    body = upgrade_run.generate_during.json()
    assert body["code"] == "upgrading" and TARGET in body["message"]


def test_rollback_returns_to_the_starting_commit_and_passes(upgrade_run, data_dir, api, baseline) -> None:
    done = comfy_command(data_dir, "rollback")
    assert done.returncode == 0, done.stdout + done.stderr
    status = api.get("/api/status").json()
    assert_checked(status, commit_prefix=baseline.commit)
    assert (status["last_operation"]["kind"], status["last_operation"]["result"]) == ("rollback", "passed")
    install = ComfyInstall(CONFIG)
    assert install.freeze() == baseline.freeze  # the snapshot taken before the upgrade, restored exactly
    assert install.branch() == baseline.branch  # back on the branch it was on, not a detached HEAD


class RoutesFailOnFirstRun:
    """Fault injection: the first run (on the upgrade target) uses a graph ComfyUI rejects (steps=0, ComfyUI's own
    validation error); later runs (the re-check after switching back) are the normal check."""

    name = RoutesCheck.name
    label = RoutesCheck.label

    def __init__(self) -> None:
        self.runs = 0

    async def run(self) -> None:
        self.runs += 1
        config = ComfyConfig(steps=0) if self.runs == 1 else CONFIG
        await RoutesCheck(ComfyBackend(config)).run()


async def test_a_failing_check_reverts_the_upgrade_automatically(data_dir, api, baseline) -> None:
    install = ComfyInstall(CONFIG)
    injected = RoutesFailOnFirstRun()
    checks = [StartsCheck(install.wait_online), NodesCheck(ComfyBackend(CONFIG)), injected]
    manager = VersionManager(install, VersionState.in_data_dir(data_dir), checks, say)
    started = time.monotonic()
    outcome = await manager.upgrade(TARGET)
    say(f"upgrade {TARGET} with an injected failure → {outcome.operation.result} in {time.monotonic() - started:.0f} s")
    operation = outcome.operation
    assert operation.result is OperationResult.REVERTED and injected.runs == 2
    assert operation.note.startswith("三条路试跑：文生图：") and "Value 0 smaller than min of 1" in operation.note
    assert baseline.commit.startswith(install.installed().commit)
    status = api.get("/api/status").json()
    assert_checked(status, commit_prefix=baseline.commit)
    last = status["last_operation"]
    assert (last["kind"], last["result"], last["to"]["tag"]) == ("upgrade", "reverted", TARGET)
    assert last["note"] == operation.note
    assert install.freeze() == baseline.freeze
    assert install.branch() == baseline.branch


def test_the_installation_ends_as_it_started(baseline) -> None:
    install = ComfyInstall(CONFIG)
    assert git("rev-parse", "HEAD") == baseline.commit and install.branch() == baseline.branch
    assert install.pinned() == baseline.pinned
    assert install.freeze() == baseline.freeze
    assert install.service_state() == "active"
