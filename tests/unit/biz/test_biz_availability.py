"""Availability: the status cache, planning without waiting on the backend, upgrades, and 503s that show at once."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from biz_fakes import VERSION, FakeBackend, make_service
from imagegen.biz.errors import Rejected, ServiceUnavailable
from imagegen.biz.models import GenerateRequest, StepRequest, UnavailableReason
from imagegen.biz.version_state import (
    CheckItem,
    LastCheck,
    Operation,
    OperationKind,
    OperationResult,
    Upgrading,
    Version,
)
from imagegen.logic.types import Route

ONE_STEP = GenerateRequest(steps=[StepRequest(instruction="画一只狐狸")])
MASK_FILE = "models/model_patches/qwen_image_2.1_fun_controlnet_union_int8_convrot.safetensors"


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
async def service(backend, tmp_path):
    async with make_service(backend, tmp_path) as service:
        yield service


async def test_status_is_cached_and_identical_while_nothing_changes(service, backend) -> None:
    first = await service.status()
    for _ in range(5):
        assert (await service.status()).model_dump_json() == first.model_dump_json()
    assert backend.status_calls == 1
    assert first.version == Version(**VERSION)
    assert (first.backend, first.host, first.online) == ("fake", "fake:0", True)
    assert first.limits.max_images == 4 and first.limits.size_multiple == 32 and first.limits.max_edge == 1920
    assert [(p.label, p.width, p.height) for p in first.size_presets] == [
        ("1:1", 1024, 1024),
        ("16:9", 1536, 864),
        ("9:16", 864, 1536),
    ]


async def test_planning_never_waits_on_the_backend_once_it_knows_something(service, backend) -> None:
    await service.status()
    calls = backend.status_calls
    for _ in range(20):
        assert (await service.plan(ONE_STEP)).ok
    assert backend.status_calls == calls


async def test_a_missing_file_blocks_only_its_route(service, backend) -> None:
    backend.lose_file(Route.MASK, MASK_FILE)
    status = await service.status()
    assert status.routes.t2i.available and status.routes.ref.available
    assert not status.routes.mask.available and MASK_FILE in status.routes.mask.reason


async def test_generate_503_shows_in_status_and_plan_at_once(service, backend) -> None:
    assert (await service.status()).online
    assert (await service.plan(ONE_STEP)).ok
    backend.go_down()
    with pytest.raises(ServiceUnavailable) as caught:
        await service.generate(ONE_STEP)
    assert caught.value.code == "comfy_down" and caught.value.raw == "connection refused (fake)"
    # No waiting for the 5 s cache: status and plan already know.
    assert not (await service.status()).online
    plan = await service.plan(ONE_STEP)
    assert not plan.ok and plan.steps[0].unavailable.code is UnavailableReason.COMFY_DOWN


async def test_rejects_come_before_unavailability(service, backend) -> None:
    backend.go_down()
    with pytest.raises(Rejected) as caught:
        await service.generate(GenerateRequest(steps=[StepRequest(instruction="")]))
    assert caught.value.plan.steps[0].reject.code == "empty_instruction"
    assert "第 1 步" in caught.value.message


async def test_an_upgrade_makes_every_route_unavailable(service, backend) -> None:
    since = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    service.version_state.set_upgrading(Upgrading(target="v0.42.5", since=since))
    plan = await service.plan(ONE_STEP)
    assert not plan.ok and plan.steps[0].unavailable.code is UnavailableReason.UPGRADING
    with pytest.raises(ServiceUnavailable) as caught:
        await service.generate(ONE_STEP)
    assert caught.value.code == "upgrading" and "升级" in caught.value.message and "v0.42.5" in caught.value.raw
    status = await service.status()
    assert status.upgrading == Upgrading(target="v0.42.5", since=since)
    assert not any(r.available for r in (status.routes.t2i, status.routes.ref, status.routes.mask))
    assert "升级" in status.routes.t2i.reason

    service.version_state.clear_upgrading()
    assert (await service.plan(ONE_STEP)).ok
    assert (await service.status()).upgrading is None


async def test_status_shows_the_last_check_and_operation(service) -> None:
    at = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    old, new = Version(tag=None, commit="0b05cd8"), Version(tag="v0.42.5", commit="483dae4")
    check = LastCheck(at=at, version=new, items=[CheckItem(name="starts", label="能启动", ok=True)])
    operation = Operation(
        at=at, kind=OperationKind.UPGRADE, from_=old, to=new, result=OperationResult.PASSED, note=None
    )
    service.version_state.set_last_check(check)
    service.version_state.append_operation(operation)
    status = await service.status()
    assert status.last_check == check and status.last_operation == operation
    wire = status.model_dump(mode="json")
    assert wire["last_operation"]["from"] == {"tag": None, "commit": "0b05cd8"}
    assert wire["last_check"]["at"] == "2026-10-26T12:00:00Z"
