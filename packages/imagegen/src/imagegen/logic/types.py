"""Value types shared by the logic layer: routes, sizes, one step's input and output."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Route(StrEnum):
    T2I = "t2i"  # text to image
    REF = "ref"  # reference-image edit; images[0] is the canvas
    MASK = "mask"  # mask rewrite; images[0] is the base image the mask is painted on


@dataclass(frozen=True, slots=True)
class Size:
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"size must be positive, got {self.width}x{self.height}")

    @property
    def long_edge(self) -> int:
        return max(self.width, self.height)

    @property
    def area(self) -> int:
        return self.width * self.height

    def __str__(self) -> str:
        return f"{self.width}x{self.height}"


# A resolved image: encoded bytes (png / jpeg / webp …) or the path of an encoded image file.
type ImageRef = bytes | Path

# Called with the step's progress in [0, 1].
type ProgressCallback = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class StepInput:
    """Everything a backend needs to run one step. The caller has already chosen the route and the output size.

    images: t2i → none; ref → the canvas first, then references; mask → the base image first, then references.
    mask:   mask route only; white = repaint, same size as images[0] (the caller binarises and checks it). A backend
            may change pixels outside the white area too; the caller pastes the original back there afterwards.
    size:   the output size; for ref / mask it is images[0]'s size after the caller's scale-down and rounding.
    """

    route: Route
    instruction: str
    images: tuple[ImageRef, ...]
    mask: ImageRef | None
    seed: int
    size: Size


@dataclass(frozen=True, slots=True)
class StepOutput:
    png: bytes
    width: int
    height: int
    seed: int
