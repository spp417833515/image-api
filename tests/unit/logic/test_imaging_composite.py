"""imaging.composite_masked: the white area is the generated image exactly, beyond the feather band the base exactly,
and across the band a monotonic, linear fade."""

from __future__ import annotations

import random

import pytest
from imagegen.logic import imaging
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

SIZE = (160, 120)


def noise(seed: int, size: tuple[int, int] = SIZE) -> Image.Image:
    """Every pixel different, so "equal exactly" means something."""
    return Image.frombytes("RGB", size, random.Random(seed).randbytes(size[0] * size[1] * 3))


def painted() -> Image.Image:
    """A mask like a user's: a blob, a patch touching the image border, an 8 px stroke and a 1 px stroke."""
    mask = Image.new("L", SIZE, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((30, 30, 80, 70), fill=255)
    draw.rectangle((140, 0, 159, 25), fill=255)
    draw.line((20, 100, 140, 100), fill=255, width=8)
    draw.line((100, 10, 100, 60), fill=255, width=1)
    return mask


def grown(mask: Image.Image, px: int) -> Image.Image:
    """Every pixel within Chebyshev distance px of the white area."""
    return mask.filter(ImageFilter.MaxFilter(2 * px + 1)) if px else mask


def largest_difference(a: Image.Image, b: Image.Image, where: Image.Image) -> int:
    stat = ImageStat.Stat(ImageChops.difference(a, b), mask=where)
    assert stat.count[0] > 0, "the region is empty"
    return max(high for _, high in stat.extrema)


@pytest.mark.parametrize("feather", [0, 2, 12])
def test_white_area_is_generated_and_beyond_the_band_is_base_exactly(feather) -> None:
    base, generated, mask = noise(1), noise(2), painted()
    result = imaging.composite_masked(base, generated, mask, feather)
    assert result.mode == "RGB" and result.size == SIZE
    assert largest_difference(result, generated, mask) == 0  # the whole white area, thin strokes included
    beyond = ImageOps.invert(grown(mask, feather))
    assert largest_difference(result, base, beyond) == 0
    if feather:
        band = ImageChops.subtract(grown(mask, feather), mask)
        assert largest_difference(result, base, band) > 0  # the band really blends


@pytest.mark.parametrize("feather", [2, 12])
def test_the_band_fades_linearly_and_monotonically_from_generated_to_base(feather) -> None:
    edge = 40
    base_colour, generated_colour = (10, 200, 40), (240, 20, 180)
    base = Image.new("RGB", (80, 9), base_colour)
    generated = Image.new("RGB", (80, 9), generated_colour)
    mask = Image.new("L", (80, 9), 0)
    ImageDraw.Draw(mask).rectangle((edge, 0, 79, 8), fill=255)
    result = imaging.composite_masked(base, generated, mask, feather)
    row = [result.getpixel((x, 4)) for x in range(80)]
    assert all(pixel == generated_colour for pixel in row[edge:])
    assert all(pixel == base_colour for pixel in row[: edge - feather])
    band = row[edge - feather : edge]  # farthest from the white area first
    for channel in range(3):
        start, end = base_colour[channel], generated_colour[channel]
        values = [start, *(pixel[channel] for pixel in band), end]
        steps = [b - a for a, b in zip(values, values[1:], strict=False)]
        assert all(step * (end - start) > 0 for step in steps), f"channel {channel} is not monotonic: {values}"
        share = [(value - start) / (end - start) for value in values[1:-1]]
        linear = [k / (feather + 1) for k in range(1, feather + 1)]
        assert all(abs(got - want) <= 1.5 / abs(end - start) for got, want in zip(share, linear, strict=True))


def test_inputs_that_cannot_be_composited_fail_loudly() -> None:
    base, generated, mask = noise(1), noise(2), painted()
    for feather in (-2, 3):
        with pytest.raises(ValueError, match="feather_px"):
            imaging.composite_masked(base, generated, mask, feather)
    with pytest.raises(ValueError, match="one size"):
        imaging.composite_masked(base, noise(2, (159, 120)), mask, 2)
    grey = mask.point(lambda v: 128 if v else 0)
    with pytest.raises(imaging.ImageError, match="binarised"):
        imaging.composite_masked(base, generated, grey, 2)
