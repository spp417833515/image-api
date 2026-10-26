"""The composition root: the only place that wires a concrete backend (ComfyUI) into the business layer."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from .biz.service import ImageGenService
from .logic.comfy.backend import ComfyBackend
from .logic.comfy.config import ComfyConfig
from .logic.comfy.version import read_installed_version


def create_service(data_dir: Path, comfy: ComfyConfig | None = None) -> ImageGenService:
    """An ImageGenService on the local ComfyUI. Use it as `async with create_service(...) as service:`."""
    config = comfy or ComfyConfig()
    return ImageGenService(ComfyBackend(config), data_dir, partial(read_installed_version, config.root))
