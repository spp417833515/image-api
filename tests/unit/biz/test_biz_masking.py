"""A mask step's stored result is the original outside the white area and the backend's output inside it (fake
backend: it repaints the whole picture in one flat colour, the worst colour shift there is)."""

from __future__ import annotations

import asyncio
import random

import pytest
from biz_fakes import FakeBackend, decoded, encode, make_service, mask_png
from imagegen.biz.models import GenerateRequest, JobState, StepRequest
from imagegen.biz.routing import MASK_FEATHER_PX, MASK_THRESHOLD
from imagegen.logic import imaging
from imagegen.logic.types import Size
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

BOX = (300, 150, 649, 399)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
async def service(backend, tmp_path):
    async with make_service(backend, tmp_path) as service:
        yield service


def noise(width: int, height: int, mode: str = "RGB") -> Image.Image:
    channels = len(mode)
    return Image.frombytes(mode, (width, height), random.Random(7).randbytes(width * height * channels))


def largest_difference(a: Image.Image, b: Image.Image, where: Image.Image) -> int:
    stat = ImageStat.Stat(ImageChops.difference(a, b), mask=where)
    assert stat.count[0] > 0, "the region is empty"
    return max(high for _, high in stat.extrema)


async def run_mask_step(service, base: bytes, mask: bytes) -> tuple[Image.Image, Image.Image]:
    """The stored result, and what the backend returned, both at the output size."""
    base_id = (await service.upload_image(base)).id
    mask_id = (await service.upload_image(mask)).id
    accepted = await service.generate(
        GenerateRequest(steps=[StepRequest(instruction="改", images=[base_id], mask=mask_id)])
    )
    job = await asyncio.wait_for(service.wait(accepted.job_id), 5)
    assert job.state is JobState.DONE and job.steps[0].route == "mask"
    stored = decoded(service.image_bytes(job.steps[0].image.id)).convert("RGB")
    return stored, Image.new("RGB", stored.size, (0, 90, 160))  # the fake's first output colour


async def test_outside_the_white_area_is_the_original_and_inside_the_backends_output(service, backend) -> None:
    original = noise(1000, 600)  # output 992×608: the original is scaled to it the way the backend got it
    stored, generated = await run_mask_step(service, encode(original, "PNG"), mask_png(1000, 600, BOX))
    assert stored.size == (992, 608) and backend.runs[0].size == Size(992, 608)
    size = Size(*stored.size)
    white = imaging.resize_mask(imaging.binarize_mask(decoded(mask_png(1000, 600, BOX)), MASK_THRESHOLD), size)
    expected_outside = imaging.resize(original, size)
    assert largest_difference(stored, generated, white) == 0
    beyond = ImageOps.invert(white.filter(ImageFilter.MaxFilter(2 * MASK_FEATHER_PX + 1)))
    assert largest_difference(stored, expected_outside, beyond) == 0
    band = ImageChops.subtract(white.filter(ImageFilter.MaxFilter(2 * MASK_FEATHER_PX + 1)), white)
    assert largest_difference(stored, expected_outside, band) > 0  # the edge is feathered, not cut


async def test_transparent_parts_of_the_original_come_back_white(service) -> None:
    original = Image.new("RGBA", (512, 512), (0, 0, 0, 0))  # fully transparent
    stored, _ = await run_mask_step(service, encode(original, "PNG"), mask_png(512, 512, (200, 200, 300, 300)))
    assert stored.getpixel((10, 10)) == (255, 255, 255)


async def test_other_routes_store_the_backends_output_unchanged(service, backend) -> None:
    base_id = (await service.upload_image(encode(noise(512, 512), "PNG"))).id
    accepted = await service.generate(GenerateRequest(steps=[StepRequest(instruction="改", images=[base_id])]))
    job = await asyncio.wait_for(service.wait(accepted.job_id), 5)
    stored = decoded(service.image_bytes(job.steps[0].image.id)).convert("RGB")
    assert job.steps[0].route == "ref"
    assert (
        largest_difference(stored, Image.new("RGB", stored.size, (0, 90, 160)), Image.new("L", stored.size, 255)) == 0
    )


async def test_the_result_is_exactly_the_composite_of_what_the_backend_was_given(service, backend) -> None:
    """Diagonal and curved mask edges at a non-integer scale (1000×600 → 992×608): any other way of bringing the
    mask or the original to the output size than the backend's (imaging.resize_mask / resize) changes edge pixels."""
    original = noise(1000, 600)
    painted = Image.new("L", (1000, 600), 0)
    draw = ImageDraw.Draw(painted)
    draw.ellipse((90, 70, 433, 371), fill=255)
    draw.polygon([(520, 40), (980, 300), (930, 380), (470, 120)], fill=255)  # a slanted band
    draw.line((60, 560, 940, 430), fill=255, width=9)
    stored, generated = await run_mask_step(service, encode(original, "PNG"), encode(painted, "PNG"))
    size = Size(*stored.size)
    assert size == Size(992, 608)
    sent = decoded(backend.runs[0].mask)  # the binarised mask the backend got, at the original's size
    expected = imaging.composite_masked(
        imaging.resize(original, size), generated, imaging.resize_mask(sent, size), MASK_FEATHER_PX
    )
    assert ImageChops.difference(stored, expected).getbbox() is None
