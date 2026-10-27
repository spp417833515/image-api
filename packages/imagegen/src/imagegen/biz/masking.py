"""The mask rules: what counts as painted, and what a mask step's result is.

A mask step repaints the white area and nothing else. Backends repaint the whole picture and may shift colours
outside the white area (measured up to 18.5/255 on solid colours), so the original is pasted back there, whatever the
backend (ppwiki decision id=5, third revision).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ..logic import imaging
from ..logic.storage import ImageStore
from .routing import MASK_FEATHER_PX, MASK_THRESHOLD


def binarized_mask(store: ImageStore, image_id: str) -> Image.Image:
    """The stored mask as the backend gets it: white (luminance ≥ MASK_THRESHOLD) = repaint, mode L, only 0 / 255."""
    return imaging.binarize_mask(imaging.decode(store.path(image_id)), MASK_THRESHOLD)


def paste_back(original: Path, mask: Image.Image, generated_png: bytes) -> bytes:
    """A mask step's result: generated in the white area, the original outside it, faded over MASK_FEATHER_PX.

    original is the step's base image and mask its binarised mask, both at the base image's size; they are brought to
    the generated image's size the way the backend got them (imaging.resize / resize_mask).
    """
    generated = imaging.decode(generated_png, {"PNG"}).convert("RGB")
    size = imaging.size_of(generated)
    base = imaging.resize(imaging.flatten(imaging.decode(original), imaging.FLATTEN_BACKGROUND), size)
    return imaging.to_png(imaging.composite_masked(base, generated, imaging.resize_mask(mask, size), MASK_FEATHER_PX))
