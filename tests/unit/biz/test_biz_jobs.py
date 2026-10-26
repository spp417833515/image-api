"""Jobs: steps in order, "$prev" hand-over, stop at the first failure, seeds, progress, history (fake backend)."""

from __future__ import annotations

import asyncio

import pytest
from biz_fakes import FakeBackend, decoded, make_service, mask_png, png
from imagegen.biz.errors import NotFound
from imagegen.biz.models import GenerateRequest, JobState, StepRequest, StepState
from imagegen.biz.routing import MAX_SAFE_SEED
from imagegen.logic.backend import (
    BackendUnavailable,
    CleanupFailed,
    StepFailed,
    Unavailable,
    UnavailableCode,
    UnexpectedResponse,
)
from imagegen.logic.types import Route, Size, StepOutput

COMFY_TEXT = "Prompt outputs failed validation\nKSampler:\n- Value 0 smaller than min of 1: steps"


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
async def service(backend, tmp_path):
    async with make_service(backend, tmp_path) as service:
        yield service


async def run(service, *steps: StepRequest, first_step: int = 1):
    accepted = await service.generate(GenerateRequest(first_step=first_step, steps=list(steps)))
    return await asyncio.wait_for(service.wait(accepted.job_id), 5)


async def test_prev_hands_the_previous_output_to_the_next_step(service, backend) -> None:
    job = await run(
        service,
        StepRequest(instruction="画一只狐狸", width=1000, height=700, seed=7),
        StepRequest(instruction="改成白狐", images=["$prev"], seed=8),
    )
    assert job.state is JobState.DONE
    first, second = job.steps
    assert (first.route, second.route) == (Route.T2I, Route.REF)
    assert backend.runs[0].size == Size(992, 704) and backend.runs[0].images == ()
    assert backend.runs[1].images == (service.image_path(first.image.id),)  # step 2's image 1 = step 1's output
    assert backend.runs[1].size == Size(992, 704)
    assert (first.image.seed, second.image.seed) == (7, 8)
    assert (first.image.width, first.image.height) == (992, 704)


async def test_a_failed_step_stops_the_job_and_keeps_earlier_images(service, backend) -> None:
    backend.failures[1] = StepFailed(COMFY_TEXT)
    job = await run(
        service,
        StepRequest(instruction="画"),
        StepRequest(instruction="改", images=["$prev"]),
        StepRequest(instruction="再改", images=["$prev"]),
        first_step=3,
    )
    assert job.state is JobState.FAILED
    assert [s.step for s in job.steps] == [3, 4, 5]
    assert [s.state for s in job.steps] == [StepState.DONE, StepState.FAILED, StepState.SKIPPED]
    assert job.steps[1].error.raw == COMFY_TEXT  # ComfyUI's text, unedited
    assert job.steps[1].image is None and job.steps[2].image is None and job.steps[2].error is None
    assert len(backend.runs) == 2  # step 5 was never sent
    kept = job.steps[0].image
    assert decoded(service.image_bytes(kept.id)).size == (kept.width, kept.height)
    assert [item.id for item in (await service.history()).items] == [kept.id]


async def test_failures_carry_the_backends_original_text(service, backend) -> None:
    down = Unavailable(UnavailableCode.BACKEND_DOWN, "ComfyUI 未运行", "cannot reach ComfyUI at x: ConnectError()")
    noted = StepFailed("ComfyUI said no")
    noted.add_note("cleaning up this run's ComfyUI files also failed: disk full")
    cases = [
        (BackendUnavailable(down), down.raw),
        (noted, "ComfyUI said no\ncleaning up this run's ComfyUI files also failed: disk full"),
        (UnexpectedResponse("bad history", "{'x': 1}"), "bad history: {'x': 1}"),
        (ValueError("mask must be binary"), "内部错误 ValueError: mask must be binary"),
    ]
    for index, (error, raw) in enumerate(cases):
        backend.failures[index] = error
        job = await run(service, StepRequest(instruction="画"))
        assert job.state is JobState.FAILED and job.steps[0].error.raw == raw, type(error)


async def test_unavailable_mid_job_refreshes_the_status(service, backend) -> None:
    backend.hold = asyncio.Event()
    backend.failures[0] = BackendUnavailable(Unavailable(UnavailableCode.BACKEND_DOWN, "down", "refused"))
    accepted = await service.generate(GenerateRequest(steps=[StepRequest(instruction="画")]))
    await asyncio.wait_for(backend.started.wait(), 5)
    calls = backend.status_calls
    backend.go_down()  # the backend went away after the job was accepted
    backend.hold.set()
    job = await asyncio.wait_for(service.wait(accepted.job_id), 5)
    assert job.state is JobState.FAILED and job.steps[0].error.raw == "refused"
    status = await service.status()  # well inside the cache's 5 s, yet already shows the backend down
    assert backend.status_calls == calls + 1
    assert not status.online and not status.routes.t2i.available


async def test_cleanup_failure_keeps_the_finished_image(service, backend) -> None:
    output = StepOutput(png(1024, 1024), 1024, 1024, 5)
    backend.failures[0] = CleanupFailed(output, "could not delete imageapi_x.png")
    job = await run(service, StepRequest(instruction="画", seed=5))
    assert job.state is JobState.DONE and job.steps[0].image.seed == 5


async def test_unset_seeds_are_drawn_and_reported(service, backend) -> None:
    job = await run(service, StepRequest(instruction="画"), StepRequest(instruction="改", images=["$prev"]))
    seeds = [s.image.seed for s in job.steps]
    assert [r.seed for r in backend.runs] == seeds
    assert all(0 <= seed <= MAX_SAFE_SEED for seed in seeds)


async def test_drawn_seeds_stay_within_the_backends_range(tmp_path) -> None:
    backend = FakeBackend(max_seed=3)
    async with make_service(backend, tmp_path) as service:
        for _ in range(8):
            await run(service, StepRequest(instruction="画", width=256, height=256))
    assert {r.seed for r in backend.runs} <= {0, 1, 2, 3}


async def test_the_mask_reaches_the_backend_binarised(service, backend) -> None:
    base = (await service.upload_image(png(640, 480))).id
    grey = (await service.upload_image(mask_png(640, 480, (0, 0, 199, 99), grey=200))).id
    job = await run(service, StepRequest(instruction="改", images=[base], mask=grey))
    assert job.state is JobState.DONE and job.steps[0].route is Route.MASK
    sent = decoded(backend.runs[0].mask)
    assert sent.mode == "L" and sent.size == (640, 480)
    assert (
        {v for v, n in enumerate(sent.histogram()) if n} == {0, 255}
        and sent.getpixel((10, 10)) == 255
        and sent.getpixel((300, 300)) == 0
    )
    assert backend.runs[0].size == Size(640, 480)


async def test_progress_and_finished_steps_are_visible_while_running(service, backend) -> None:
    backend.hold = asyncio.Event()
    backend.failures = {}
    accepted = await service.generate(
        GenerateRequest(steps=[StepRequest(instruction="画"), StepRequest(instruction="改", images=["$prev"])])
    )
    await asyncio.wait_for(backend.started.wait(), 5)
    running = service.job(accepted.job_id)
    assert running.state is JobState.RUNNING
    assert [(s.state, s.progress) for s in running.steps] == [(StepState.RUNNING, 0.5), (StepState.QUEUED, None)]

    backend.started.clear()
    backend.hold.set()
    backend.hold = asyncio.Event()  # the second step waits again
    await asyncio.wait_for(backend.started.wait(), 5)
    middle = service.job(accepted.job_id)
    assert middle.steps[0].state is StepState.DONE and middle.steps[1].state is StepState.RUNNING
    # A step reported done is already in the history.
    assert (await service.history()).items[0].id == middle.steps[0].image.id
    backend.hold.set()
    assert (await asyncio.wait_for(service.wait(accepted.job_id), 5)).state is JobState.DONE


async def test_jobs_run_one_at_a_time_without_anyone_polling(service, backend) -> None:
    ids = [
        (await service.generate(GenerateRequest(steps=[StepRequest(instruction=f"画 {n}")]))).job_id for n in range(3)
    ]
    assert service.job(ids[2]).state is JobState.QUEUED
    for job_id in ids:
        await asyncio.wait_for(service.wait(job_id), 5)
    assert [r.instruction for r in backend.runs] == ["画 0", "画 1", "画 2"]
    history = (await service.history()).items
    assert [(h.instruction, h.job_id, h.step) for h in history] == [(f"画 {n}", ids[n], 1) for n in (2, 1, 0)]


async def test_stopping_the_service_ends_unfinished_jobs(tmp_path) -> None:
    backend = FakeBackend()
    backend.hold = asyncio.Event()
    service = make_service(backend, tmp_path)
    await service.start()
    accepted = await service.generate(
        GenerateRequest(steps=[StepRequest(instruction="画"), StepRequest(instruction="改", images=["$prev"])])
    )
    await asyncio.wait_for(backend.started.wait(), 5)
    await service.stop()
    job = service.job(accepted.job_id)
    assert job.state is JobState.FAILED
    assert [s.state for s in job.steps] == [StepState.FAILED, StepState.SKIPPED]
    assert job.steps[0].error.raw


async def test_unknown_job_and_unstarted_service(tmp_path) -> None:
    service = make_service(FakeBackend(), tmp_path)
    with pytest.raises(NotFound):
        service.job("job_missing")
    with pytest.raises(RuntimeError, match="not started"):
        await service.generate(GenerateRequest(steps=[StepRequest(instruction="画")]))
