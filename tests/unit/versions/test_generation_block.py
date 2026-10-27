"""What the service says while an upgrade runs, after one was interrupted, and with none: /api/status, /api/plan and
/api/generate over HTTP (FastAPI TestClient, fake backend), each response checked with the page's own shapes.js."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from imagegen.biz.models import GenerateRequest, StepRequest, StepState
from imagegen.biz.version_state import RECOVER_COMMAND, Upgrading, Version
from versions_fakes import kill_recorder

# The fake backend is shared with the business-layer tests, the page's validator with the web tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "biz"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from biz_fakes import FakeBackend, make_service  # noqa: E402
from page_shapes import assert_page_accepts  # noqa: E402

from image_api.web.app import create_app  # noqa: E402

SINCE = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
ONE_STEP = {"steps": [{"instruction": "画一只赤狐"}]}
ORIGIN = Version(tag=None, commit="0b05cd8a")


@pytest.fixture
def service(tmp_path):
    return make_service(FakeBackend(), tmp_path)


@pytest.fixture
def client(service):
    with TestClient(create_app(service)) as client:
        yield client


def answers(client: TestClient) -> tuple[dict, dict, object]:
    status = client.get("/api/status").json()
    plan = client.post("/api/plan", json=ONE_STEP).json()
    generate = client.post("/api/generate", json=ONE_STEP)
    assert_page_accepts([("status", status), ("plan", plan)])
    return status, plan, generate


def test_no_upgrade_lets_generation_run(client) -> None:
    status, plan, generate = answers(client)
    assert status["upgrading"] is None and status["routes"]["t2i"] == {"available": True, "reason": None}
    assert plan["ok"] and plan["steps"][0]["unavailable"] is None
    assert generate.status_code == 202


def test_a_running_upgrade_is_503_upgrading(client, service) -> None:
    service.version_state.set_upgrading(
        Upgrading(target="v0.42.5", since=SINCE), origin=Version(tag=None, commit="0b05cd8a")
    )
    status, plan, generate = answers(client)
    assert status["upgrading"] == {"target": "v0.42.5", "since": "2026-10-26T12:00:00Z"}
    assert "正在升级到 v0.42.5" in status["routes"]["mask"]["reason"]
    assert plan["steps"][0]["unavailable"]["code"] == "upgrading"
    assert generate.status_code == 503 and generate.json()["code"] == "upgrading"
    assert "正在升级到 v0.42.5" in generate.json()["message"]


def test_an_interrupted_upgrade_is_503_upgrading_pointing_to_recover(client, service) -> None:
    service.version_state.set_upgrading(
        Upgrading(target="v0.42.5", since=SINCE), origin=Version(tag=None, commit="0b05cd8a")
    )
    kill_recorder(service.version_state)  # the upgrading process is gone
    status, plan, generate = answers(client)
    assert status["upgrading"] is None  # not "upgrading now" forever
    started = SINCE.astimezone().strftime("%Y-%m-%d %H:%M")
    message = f"上次升级到 v0.42.5 中断了（{started} 开始），ComfyUI 停在未检查的状态：运行 {RECOVER_COMMAND} 恢复"
    assert {route["reason"] for route in status["routes"].values()} == {message}
    assert plan["steps"][0]["unavailable"] == {"code": "upgrading", "message": message}
    assert generate.status_code == 503
    body = generate.json()
    assert (body["code"], body["message"]) == ("upgrading", message)
    assert RECOVER_COMMAND in body["raw"] and "interrupted" in body["raw"]


def test_an_upgrade_by_a_process_that_really_exited_reads_as_interrupted(client, service) -> None:
    writer = textwrap.dedent(f"""
        from datetime import datetime, UTC
        from pathlib import Path
        from imagegen.biz.version_state import Upgrading, VersionState
        VersionState(Path({str(service.version_state.directory)!r})).set_upgrading(
            Upgrading(target="v0.42.5", since=datetime(2026, 10, 26, 12, tzinfo=UTC)))
    """)
    subprocess.run([sys.executable, "-c", writer], check=True)
    status, _, generate = answers(client)
    assert status["upgrading"] is None and "中断了" in status["routes"]["t2i"]["reason"]
    assert generate.status_code == 503 and "中断了" in generate.json()["message"]


async def test_after_an_interrupted_upgrade_the_next_step_of_a_job_does_not_run(tmp_path) -> None:
    """The job runner follows the same rule as /api/generate (VersionState.generation_block)."""
    backend = FakeBackend()
    backend.hold = asyncio.Event()
    async with make_service(backend, tmp_path) as service:
        two_steps = [StepRequest(instruction="画"), StepRequest(instruction="改", images=["$prev"])]
        accepted = await service.generate(GenerateRequest(steps=two_steps))
        await asyncio.wait_for(backend.started.wait(), 5)
        service.version_state.set_upgrading(Upgrading(target="v0.42.5", since=SINCE), origin=ORIGIN)
        kill_recorder(service.version_state)  # the upgrade began during step 1, then its process died
        backend.hold.set()
        job = await asyncio.wait_for(service.wait(accepted.job_id), 5)
    assert [step.state for step in job.steps] == [StepState.DONE, StepState.FAILED]
    assert job.steps[1].error.raw == f"上次升级到 v0.42.5 中断了，这一步没有运行：运行 {RECOVER_COMMAND} 恢复"
    assert len(backend.runs) == 1  # step 2 never reached the backend
