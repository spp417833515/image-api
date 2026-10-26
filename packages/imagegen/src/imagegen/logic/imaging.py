"""Image decoding, validation, sizing and mask processing. Pure functions; every limit and threshold is a parameter."""

from __future__ import annotations

import io
import math
from collections.abc import Collection
from pathlib import Path

from PIL import Image, ImageOps

from .types import ImageRef, Size


class ImageError(ValueError):
    """The input is not a usable image."""


def decode(source: ImageRef, formats: Collection[str] | None = None) -> Image.Image:
    """Decode and fully load an image with its EXIF orientation applied.

    formats restricts the accepted Pillow format names (e.g. {"PNG", "JPEG", "WEBP"}); None accepts any readable image.
    Raises ImageError saying what was received.
    """
    data = source.read_bytes() if isinstance(source, Path) else source
    what = f"{source}" if isinstance(source, Path) else f"{len(data)} bytes starting {data[:12]!r}"
    try:
        with Image.open(io.BytesIO(data), formats=None if formats is None else list(formats)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        expected = "an image" if formats is None else "one of " + ", ".join(sorted(formats))
        raise ImageError(f"expected {expected}, got {what}: {exc}") from exc
    return image


def size_of(image: Image.Image) -> Size:
    return Size(*image.size)


def scale_down(size: Size, max_edge: int) -> Size:
    """Shrink so the long edge is at most max_edge, keeping the aspect ratio. Never enlarges."""
    if max_edge <= 0:
        raise ValueError(f"max_edge must be positive, got {max_edge}")
    if size.long_edge <= max_edge:
        return size
    factor = max_edge / size.long_edge
    if size.width >= size.height:
        return Size(max_edge, max(1, round(size.height * factor)))
    return Size(max(1, round(size.width * factor)), max_edge)


def scale_to_area(size: Size, max_area: int) -> Size:
    """Shrink so width × height is at most max_area, keeping the aspect ratio. Never enlarges."""
    if max_area <= 0:
        raise ValueError(f"max_area must be positive, got {max_area}")
    if size.area <= max_area:
        return size
    factor = math.sqrt(max_area / size.area)
    return Size(max(1, math.floor(size.width * factor)), max(1, math.floor(size.height * factor)))


def round_to_multiple(size: Size, multiple: int) -> Size:
    """Round each side to the nearest multiple (halves round up), at least one multiple: 960×540 → 960×544 for 32."""
    if multiple <= 0:
        raise ValueError(f"multiple must be positive, got {multiple}")

    def nearest(value: int) -> int:
        return max(multiple, (value + multiple // 2) // multiple * multiple)

    return Size(nearest(size.width), nearest(size.height))


def flatten(image: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """RGB image with any transparency composited over background."""
    if image.mode == "RGB":
        return image
    if "A" not in image.getbands() and "transparency" not in image.info:
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    base = Image.new("RGBA", rgba.size, (*background, 255))
    return Image.alpha_composite(base, rgba).convert("RGB")


def binarize_mask(image: Image.Image, threshold: float) -> Image.Image:
    """Mode "L" mask: 255 where luminance ≥ threshold (0–1), else 0. Transparent pixels count as black (keep)."""
    if not 0.0 < threshold <= 1.0:
        raise ValueError(f"threshold must be in (0, 1], got {threshold}")
    cut = threshold * 255
    table = [255 if level >= cut else 0 for level in range(256)]
    return flatten(image, (0, 0, 0)).convert("L").point(table)


def is_all_black(mask: Image.Image) -> bool:
    """True when no pixel is lit, i.e. the mask asks to repaint nothing. Expects a binarised mask."""
    return mask.convert("L").getextrema()[1] == 0


def require_binary_mask(mask: Image.Image) -> None:
    """Raise ImageError unless mask is what binarize_mask produces: one channel (mode L or 1), only 0 and 255."""
    if mask.mode not in ("L", "1"):
        raise ImageError(f"expected a binarised mask (mode L, only 0 and 255; see binarize_mask), got mode {mask.mode}")
    levels = [level for level, count in enumerate(mask.convert("L").histogram()) if count]
    grey = [level for level in levels if level not in (0, 255)]
    if grey:
        raise ImageError(
            f"expected a binarised mask (only 0 and 255; see binarize_mask), got {len(grey)} other level(s) "
            f"between {grey[0]} and {grey[-1]}"
        )


def resize(image: Image.Image, size: Size) -> Image.Image:
    if image.size == (size.width, size.height):
        return image
    return image.resize((size.width, size.height), Image.Resampling.LANCZOS)


def resize_mask(mask: Image.Image, size: Size) -> Image.Image:
    """Nearest-neighbour, so a binary mask stays binary."""
    if mask.size == (size.width, size.height):
        return mask
    return mask.resize((size.width, size.height), Image.Resampling.NEAREST)


def to_png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
