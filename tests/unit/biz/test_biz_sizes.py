"""Output sizes, size and seed ranges, and the product limits against the backend's capabilities."""

from __future__ import annotations

import pytest
from biz_fakes import FakeBackend, make_service, mask_png, png
from imagegen.biz.models import GenerateRequest, ImageSize, RejectCode, StepRequest
from imagegen.biz.routing import MAX_SAFE_SEED, Limits
from imagegen.logic.backend import BackendCapabilities
from imagegen.logic.types import Route


@pytest.fixture
async def service(tmp_path):
    async with make_service(FakeBackend(), tmp_path) as service:
        yield service


async def plan_one(service, **step):
    return (await service.plan(GenerateRequest(steps=[StepRequest(**({"instruction": "画"} | step))]))).steps[0]


def size(width: int, height: int) -> ImageSize:
    return ImageSize(width=width, height=height)


async def test_text_to_image_size_is_rounded_and_the_request_reported(service) -> None:
    step = await plan_one(service, width=1000, height=700)
    assert (step.route, step.output, step.requested) == (Route.T2I, size(992, 704), size(1000, 700))


async def test_text_to_image_defaults_and_exact_sizes_report_no_request(service) -> None:
    default = await plan_one(service)
    assert (default.output, default.requested) == (size(1024, 1024), None)
    exact = await plan_one(service, width=1536, height=864)
    assert (exact.output, exact.requested) == (size(1536, 864), None)
    half = await plan_one(service, width=512)  # the side not given keeps its default
    assert (half.output, half.requested) == (size(512, 1024), None)


async def test_text_to_image_size_out_of_range_is_rejected_not_clamped(service) -> None:
    for width, height in ((1936, 1024), (2048, 1024), (1024, 239), (100, 100), (0, 512), (-64, 512)):
        step = await plan_one(service, width=width, height=height)
        assert step.reject is not None and step.reject.code is RejectCode.SIZE_OUT_OF_RANGE, (width, height)
        assert f"{width}×{height}" in step.reject.message
    edge = await plan_one(service, width=1935, height=241)  # rounds to 1920×256: in range
    assert (edge.output, edge.requested) == (size(1920, 256), size(1935, 241))


async def test_edit_output_follows_the_first_image(service) -> None:
    sheet = (await service.upload_image(png(960, 540))).id
    big = (await service.upload_image(png(3840, 2160))).id
    tall = (await service.upload_image(png(1000, 3000))).id
    ref = await plan_one(service, images=[sheet])
    assert (ref.route, ref.output, ref.requested) == (Route.REF, size(960, 544), None)
    assert (await plan_one(service, images=[big])).output == size(1920, 1088)  # scaled to 1920×1080, then rounded
    assert (await plan_one(service, images=[tall])).output == size(640, 1920)
    mask = (await service.upload_image(mask_png(960, 540, (0, 0, 50, 50)))).id
    assert (await plan_one(service, images=[sheet], mask=mask)).output == size(960, 544)


async def test_prev_output_is_predicted_from_the_previous_step(service) -> None:
    plan = await service.plan(
        GenerateRequest(
            steps=[
                StepRequest(instruction="画", width=1000, height=700),
                StepRequest(instruction="改", images=["$prev"]),
            ]
        )
    )
    assert plan.ok
    assert [s.output for s in plan.steps] == [size(992, 704), size(992, 704)]


async def test_a_mask_on_prev_must_fit_the_predicted_image(service) -> None:
    fits = (await service.upload_image(mask_png(992, 704, (0, 0, 99, 99)))).id
    wrong = (await service.upload_image(mask_png(1024, 1024, (0, 0, 99, 99)))).id
    first = StepRequest(instruction="画", width=1000, height=700)
    for mask, expected in ((fits, None), (wrong, RejectCode.MASK_SIZE_MISMATCH)):
        plan = await service.plan(
            GenerateRequest(steps=[first, StepRequest(instruction="改", images=["$prev"], mask=mask)])
        )
        reject = plan.steps[1].reject
        assert (reject and reject.code) == expected
    assert "992×704" in reject.message and "1024×1024" in reject.message


async def test_grey_mask_pixels_count_by_the_half_luminance_threshold(service) -> None:
    base = (await service.upload_image(png(256, 256))).id
    dark = (await service.upload_image(mask_png(256, 256, (0, 0, 99, 99), grey=127))).id
    light = (await service.upload_image(mask_png(256, 256, (0, 0, 99, 99), grey=128))).id
    assert (await plan_one(service, images=[base], mask=dark)).reject.code is RejectCode.MASK_EMPTY
    assert (await plan_one(service, images=[base], mask=light)).route is Route.MASK


async def test_seed_range(service) -> None:
    assert (await plan_one(service, seed=0)).reject is None
    assert (await plan_one(service, seed=MAX_SAFE_SEED)).reject is None
    for seed in (-1, MAX_SAFE_SEED + 1, 2**64):
        assert (await plan_one(service, seed=seed)).reject.code is RejectCode.SEED_OUT_OF_RANGE, seed


async def test_seed_range_is_narrowed_by_the_backend(tmp_path) -> None:
    async with make_service(FakeBackend(max_seed=1000), tmp_path) as service:
        assert (await plan_one(service, seed=1000)).reject is None
        assert (await plan_one(service, seed=1001)).reject.code is RejectCode.SEED_OUT_OF_RANGE


def test_product_limits_that_do_not_fit_the_backend_fail_at_startup(tmp_path) -> None:
    with pytest.raises(ValueError, match="not a multiple of 100"):
        make_service(FakeBackend(size_multiple=100), tmp_path)
    with pytest.raises(ValueError, match="max_images is 0"):
        Limits.for_backend(BackendCapabilities(frozenset(Route), max_images=0, size_multiple=32, max_seed=1))
    limits = Limits.for_backend(BackendCapabilities(frozenset(Route), max_images=16, size_multiple=16, max_seed=2**64))
    assert (limits.max_images, limits.size_multiple, limits.max_seed) == (4, 16, MAX_SAFE_SEED)
