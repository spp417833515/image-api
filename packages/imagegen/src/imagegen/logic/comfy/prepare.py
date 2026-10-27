"""Turn a step's images into the PNGs uploaded to ComfyUI, sized so ComfyUI resizes nothing."""

from __future__ import annotations

from dataclasses import dataclass

from .. import imaging
from ..types import StepInput

WHITE = imaging.FLATTEN_BACKGROUND


@dataclass(frozen=True, slots=True)
class Uploads:
    images: tuple[bytes, ...]  # PNG, StepInput order; images[0] (canvas / base) is exactly step.size
    mask: bytes | None  # PNG, mode L, only 0 / 255, exactly step.size


def prepare(step: StepInput, size_multiple: int) -> Uploads:
    """Canvas / base → step.size. References → at most the canvas's pixel count, at multiples of size_multiple.
    Mask → checked to be binary (0 / 255) and the base's size, then resized nearest-neighbour with it.

    Transparency in images is flattened over white, as TextEncodeQwenImage21 shows it to the vision tower.
    """
    if step.size.width % size_multiple or step.size.height % size_multiple:
        raise ValueError(f"output size must be a multiple of {size_multiple}, got {step.size}")
    if not step.images:
        if step.mask is not None:
            raise ValueError("a mask needs a base image, got a mask and no images")
        return Uploads((), None)

    canvas = imaging.decode(step.images[0])
    uploads = [imaging.to_png(imaging.resize(imaging.flatten(canvas, WHITE), step.size))]
    for ref in step.images[1:]:
        image = imaging.flatten(imaging.decode(ref), WHITE)
        fitted = imaging.scale_to_area(imaging.size_of(image), step.size.area)
        uploads.append(imaging.to_png(imaging.resize(image, imaging.round_to_multiple(fitted, size_multiple))))

    mask = None
    if step.mask is not None:
        decoded = imaging.decode(step.mask)
        imaging.require_binary_mask(decoded)  # binarising is the caller's decision (threshold), not guessed here
        if decoded.size != canvas.size:
            raise ValueError(
                f"mask must be the base image's size {imaging.size_of(canvas)}, got {imaging.size_of(decoded)}"
            )
        mask = imaging.to_png(imaging.resize_mask(decoded.convert("L"), step.size))
    return Uploads(tuple(uploads), mask)
