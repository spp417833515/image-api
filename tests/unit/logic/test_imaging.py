"""imaging: decoding / validation, scale-down, rounding to 32, mask threshold, all-black and size checks."""

from __future__ import annotations

import io

import pytest
from imagegen.logic import imaging
from imagegen.logic.types import Size
from PIL import Image


def encode(image: Image.Image, fmt: str = "PNG", **params) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **params)
    return out.getvalue()


def row(image: Image.Image) -> list:
    return [image.getpixel((x, 0)) for x in range(image.width)]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (Size(960, 540), Size(960, 544)),
        (Size(1000, 700), Size(992, 704)),
        (Size(1024, 1024), Size(1024, 1024)),
        (Size(16, 48), Size(32, 64)),  # halves round up
        (Size(5, 5), Size(32, 32)),  # never below one multiple
    ],
)
def test_round_to_multiple_of_32(given: Size, expected: Size) -> None:
    assert imaging.round_to_multiple(given, 32) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (Size(4000, 3000), Size(1920, 1440)),
        (Size(3000, 4000), Size(1440, 1920)),
        (Size(1920, 1080), Size(1920, 1080)),
        (Size(800, 600), Size(800, 600)),  # never enlarges
    ],
)
def test_scale_down_long_edge(given: Size, expected: Size) -> None:
    assert imaging.scale_down(given, 1920) == expected


def test_scale_down_then_round_matches_the_contract_example() -> None:
    assert imaging.round_to_multiple(imaging.scale_down(Size(3840, 2160), 1920), 32) == Size(1920, 1088)


def test_scale_to_area_keeps_aspect_and_never_enlarges() -> None:
    shrunk = imaging.scale_to_area(Size(4000, 2000), 1_000_000)
    assert shrunk.area <= 1_000_000
    assert abs(shrunk.width / shrunk.height - 2.0) < 0.01
    assert imaging.scale_to_area(Size(500, 500), 1_000_000) == Size(500, 500)


@pytest.mark.parametrize("bad", [0, -32])
def test_sizing_rejects_non_positive_limits(bad: int) -> None:
    with pytest.raises(ValueError):
        imaging.round_to_multiple(Size(10, 10), bad)
    with pytest.raises(ValueError):
        imaging.scale_down(Size(10, 10), bad)


def test_decode_reads_bytes_and_paths(tmp_path) -> None:
    data = encode(Image.new("RGB", (40, 30), "red"))
    path = tmp_path / "a.png"
    path.write_bytes(data)
    assert imaging.size_of(imaging.decode(data)) == Size(40, 30)
    assert imaging.size_of(imaging.decode(path)) == Size(40, 30)


def test_decode_fails_loudly_on_garbage() -> None:
    with pytest.raises(imaging.ImageError, match=r"expected an image, got 9 bytes starting b'not an im"):
        imaging.decode(b"not an im")


def test_decode_rejects_truncated_image() -> None:
    data = encode(Image.new("RGB", (64, 64), "blue"), "JPEG")
    with pytest.raises(imaging.ImageError):
        imaging.decode(data[: len(data) // 2])


def test_decode_restricts_formats() -> None:
    gif = encode(Image.new("RGB", (8, 8)), "GIF")
    with pytest.raises(imaging.ImageError, match="one of JPEG, PNG, WEBP"):
        imaging.decode(gif, formats={"PNG", "JPEG", "WEBP"})
    webp = encode(Image.new("RGB", (8, 8)), "WEBP")
    assert imaging.decode(webp, formats={"PNG", "JPEG", "WEBP"}).size == (8, 8)


def test_decode_applies_exif_orientation() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    data = encode(Image.new("RGB", (60, 20)), "JPEG", exif=exif)
    assert imaging.size_of(imaging.decode(data)) == Size(20, 60)


def test_mask_threshold_is_half_luminance() -> None:
    mask = Image.new("L", (3, 1))
    for x, level in enumerate([127, 128, 255]):
        mask.putpixel((x, 0), level)
    assert row(imaging.binarize_mask(mask, 0.5)) == [0, 255, 255]


def test_mask_transparent_pixels_count_as_keep() -> None:
    mask = Image.new("RGBA", (2, 1))
    mask.putpixel((0, 0), (255, 255, 255, 0))
    mask.putpixel((1, 0), (255, 255, 255, 255))
    assert row(imaging.binarize_mask(mask, 0.5)) == [0, 255]


def test_mask_threshold_must_be_a_fraction() -> None:
    with pytest.raises(ValueError):
        imaging.binarize_mask(Image.new("L", (1, 1)), 128)


def test_all_black_mask() -> None:
    black = imaging.binarize_mask(Image.new("RGB", (16, 16), (100, 100, 100)), 0.5)
    assert imaging.is_all_black(black)
    lit = black.copy()
    lit.putpixel((3, 3), 255)
    assert not imaging.is_all_black(lit)


def test_mask_size_mismatch_is_visible_through_size_of() -> None:
    base = imaging.decode(encode(Image.new("RGB", (960, 540))))
    mask = imaging.decode(encode(Image.new("L", (960, 544))))
    assert imaging.size_of(mask) != imaging.size_of(base)


def test_flatten_puts_transparency_over_the_background() -> None:
    clear = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    assert imaging.flatten(clear, (255, 255, 255)).getpixel((0, 0)) == (255, 255, 255)
    assert imaging.flatten(Image.new("L", (1, 1), 7), (255, 255, 255)).getpixel((0, 0)) == (7, 7, 7)


def test_resize_and_png_round_trip() -> None:
    image = imaging.resize(Image.new("RGB", (960, 540), "green"), Size(960, 544))
    data = imaging.to_png(image)
    assert data.startswith(b"\x89PNG")
    assert imaging.size_of(imaging.decode(data, formats={"PNG"})) == Size(960, 544)
