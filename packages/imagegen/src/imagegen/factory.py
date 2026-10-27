"""The composition root: the only place that wires a concrete backend (ComfyUI) into the business layer."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

from .biz.service import ImageGenService
from .biz.version_checks import default_checks
from .biz.version_state import VersionState
from .biz.versions import VersionManager
from .logic.comfy.backend import ComfyBackend
from .logic.comfy.config import ComfyConfig
from .logic.comfy.install import ComfyInstall
from .logic.comfy.version import read_installed_version


def create_service(data_dir: Path, comfy: ComfyConfig | None = None) -> ImageGenService:
    """An ImageGenService on the local ComfyUI. Use it as `async with create_service(...) as service:`."""
    config = comfy or ComfyConfig()
    return ImageGenService(ComfyBackend(config), data_dir, partial(read_installed_version, config.root))


def create_version_manager(
    data_dir: Path, comfy: ComfyConfig | None = None, report: Callable[[str], None] = lambda _: None
) -> VersionManager:
    """Upgrade / rollback / recover of the local ComfyUI installation, sharing data_dir's version state with the
    service. report receives one line per stage."""
    config = comfy or ComfyConfig()
    install = ComfyInstall(config)
    checks = default_checks(install.wait_online, ComfyBackend(config))
    return VersionManager(install, VersionState.in_data_dir(data_dir), checks, report)
