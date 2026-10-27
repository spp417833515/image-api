"""The route table and the product limits: the single source for both.

Routing looks only at how many images a step has ("$prev" counts as one) and whether it has a mask. It never reads
the instruction. The limits that apply at run time are the product limits narrowed by what the backend can do.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from ..logic import imaging
from ..logic.backend import BackendCapabilities
from ..logic.types import Route, Size

PREV = "$prev"  # in a step's images: the previous step's output in the same request

MAX_IMAGES = 4
MAX_EDGE = 1920  # ref / mask: a first image with a longer edge is scaled down first; t2i: the largest side
MIN_EDGE = 256  # t2i: the smallest side
DEFAULT_SIZE = Size(1024, 1024)
SIZE_PRESETS: tuple[tuple[str, Size], ...] = (
    ("1:1", Size(1024, 1024)),
    ("16:9", Size(1536, 864)),
    ("9:16", Size(864, 1536)),
)
MAX_SAFE_SEED = 2**53 - 1  # larger integers lose precision in the page's JavaScript numbers
MASK_THRESHOLD = 0.5  # mask pixels with luminance ≥ this are repainted
# Mask route: the original is pasted back outside the white area (ppwiki decision id=5, third revision); the model's
# output fades into it over this many pixels outside the white area (the white area itself stays all model output).
# 12: the largest shift measured outside the white area (18.5/255, on solid colours) then changes by about 1.5 levels
# per pixel, too gradual to show as a seam, where a hard edge shows a line. It costs no part of the edit (the band lies
# outside the white area, so even the page's thinnest 8 px stroke stays whole) and reaches only 12 px past what was
# painted, about 1% of a 1024 px side.
MASK_FEATHER_PX = 12
PASTE_BACKGROUND = (255, 255, 255)  # transparent parts of the original are pasted back on white

ROUTE_NAMES = {Route.T2I: "文生图", Route.REF: "参考图编辑", Route.MASK: "蒙版改写"}


def route_for(image_count: int, has_mask: bool) -> Route | None:
    """The route table. None: a mask with no image to paint it on. The image limit is checked separately."""
    if image_count == 0:
        return None if has_mask else Route.T2I
    return Route.MASK if has_mask else Route.REF


@dataclass(frozen=True, slots=True)
class Limits:
    """The limits in force: the product's, narrowed by the backend's capabilities."""

    routes: frozenset[Route]
    max_images: int
    size_multiple: int
    max_seed: int
    max_edge: int = MAX_EDGE
    min_edge: int = MIN_EDGE
    default_size: Size = DEFAULT_SIZE
    presets: tuple[tuple[str, Size], ...] = SIZE_PRESETS

    @classmethod
    def for_backend(cls, capabilities: BackendCapabilities) -> Limits:
        """Raises ValueError when the product limits cannot be met by this backend (checked once, at startup)."""
        multiple = capabilities.size_multiple
        problems = []
        sizes = (("default size", DEFAULT_SIZE), *((f"preset {label}", size) for label, size in SIZE_PRESETS))
        for label, size in sizes:
            if not MIN_EDGE <= min(size.width, size.height) <= max(size.width, size.height) <= MAX_EDGE:
                problems.append(f"{label} {size} is outside {MIN_EDGE}–{MAX_EDGE}")
        if multiple <= 0:
            problems.append(f"size_multiple must be positive, got {multiple}")
        else:
            sides = {"max edge": MAX_EDGE, "min edge": MIN_EDGE}
            for label, size in sizes:
                sides |= {f"{label} width": size.width, f"{label} height": size.height}
            problems += [
                f"{label} {side} is not a multiple of {multiple}" for label, side in sides.items() if side % multiple
            ]
        if capabilities.max_seed < 0:
            problems.append(f"max_seed must be at least 0, got {capabilities.max_seed}")
        if capabilities.routes - {Route.T2I} and capabilities.max_images < 1:
            problems.append(
                f"routes {sorted(capabilities.routes)} take images, but max_images is {capabilities.max_images}"
            )
        if problems:
            raise ValueError("the product limits do not fit the backend's capabilities: " + "; ".join(problems))
        return cls(
            routes=capabilities.routes,
            max_images=min(MAX_IMAGES, capabilities.max_images),
            size_multiple=multiple,
            max_seed=min(MAX_SAFE_SEED, capabilities.max_seed),
        )

    def random_seed(self) -> int:
        return secrets.randbelow(self.max_seed + 1)

    def edit_size(self, first_image: Size) -> Size:
        """ref / mask output: the first image, long edge scaled down to max_edge, rounded to the size multiple."""
        return imaging.round_to_multiple(imaging.scale_down(first_image, self.max_edge), self.size_multiple)

    def text_to_image_size(self, width: int, height: int) -> Size | None:
        """t2i output for a requested size: rounded to the size multiple; None when a side ends up out of range."""
        if width <= 0 or height <= 0:
            return None
        size = imaging.round_to_multiple(Size(width, height), self.size_multiple)
        if not self.min_edge <= min(size.width, size.height) <= max(size.width, size.height) <= self.max_edge:
            return None
        return size
