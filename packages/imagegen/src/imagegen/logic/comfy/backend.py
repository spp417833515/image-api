"""ComfyBackend: the image-generation backend implemented on a running ComfyUI.

ComfyUI must run on this machine from ComfyConfig.root: ComfyUI has no HTTP API to delete files, so what a run
uploads and generates is removed from <root>/input and <root>/output directly. A remote ComfyUI is not supported.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from .. import imaging
from ..backend import (
    BackendCapabilities,
    BackendStatus,
    BackendUnavailable,
    CleanupFailed,
    StepFailed,
    Unavailable,
    UnavailableCode,
    UnexpectedResponse,
)
from ..types import ProgressCallback, Route, Size, StepInput, StepOutput
from . import graphs
from .availability import check_route, check_routes
from .client import ComfyClient, ComfyHTTPError, ComfyUnreachable
from .config import ComfyConfig
from .files import CleanupError, remove_run_files
from .prepare import prepare
from .prompt import run_prompt

SIZE_MULTIPLE = 32  # Qwen Image 2.1: 16× VAE downsampling, 2×2 latent patches
MAX_SEED = 2**64 - 1  # KSampler's seed range


@dataclass
class _RunFiles:
    """What one run has put into ComfyUI's folders so far (all named <token>_*)."""

    token: str
    uploaded: int = 0
    outputs: int = 0


class ComfyBackend:
    """ImageBackend on ComfyUI. See ImageBackend for what run() and status() return and raise."""

    name = "comfyui"

    def __init__(self, config: ComfyConfig | None = None) -> None:
        self.config = config or ComfyConfig()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            routes=frozenset(Route),
            max_images=graphs.MAX_ENCODER_IMAGES,
            size_multiple=SIZE_MULTIPLE,
            max_seed=MAX_SEED,
        )

    async def status(self) -> BackendStatus:
        try:
            async with ComfyClient(self.config) as client:
                stats = await client.system_stats()
                info = await client.object_info()
        except ComfyUnreachable as exc:
            return self._offline(self._down(exc))
        except ComfyHTTPError as exc:
            return self._offline(self._erroring(exc))
        return BackendStatus(self.name, self.config.host, True, _version(stats), check_routes(info, self.config))

    async def run(self, step: StepInput, on_progress: ProgressCallback | None = None) -> StepOutput:
        if not 0 <= step.seed <= MAX_SEED:
            raise ValueError(f"seed must be in [0, {MAX_SEED}], got {step.seed}")
        # Decoding / resizing large images takes seconds of CPU: keep it off the event loop.
        uploads = await asyncio.to_thread(prepare, step, SIZE_MULTIPLE)
        run_files = _RunFiles(f"{self.config.file_prefix}{uuid.uuid4().hex[:16]}")
        files = {f"{run_files.token}_image{number}.png": png for number, png in enumerate(uploads.images, start=1)}
        mask_name = None
        if uploads.mask is not None:
            mask_name = f"{run_files.token}_mask.png"
            files[mask_name] = uploads.mask
        # Built before any I/O: it also rejects images / mask that do not fit the route.
        graph = graphs.build(
            self.config,
            step.route,
            prompt=step.instruction,
            seed=step.seed,
            size=step.size,
            images=[name for name in files if name != mask_name],
            mask=mask_name,
            output_prefix=f"{run_files.token}_out",
        )
        try:
            png, size = await self._generate(step.route, graph, files, run_files, on_progress)
        except BaseException as failure:
            self._clean_up_after(failure, run_files)
            raise
        output = StepOutput(png=png, width=size.width, height=size.height, seed=step.seed)
        try:
            remove_run_files(self.config, run_files.token, uploaded=run_files.uploaded, outputs=run_files.outputs)
        except (CleanupError, OSError) as exc:
            raise CleanupFailed(output, str(exc)) from exc
        if on_progress is not None:
            on_progress(1.0)
        return output

    async def _generate(
        self,
        route: Route,
        graph: graphs.Graph,
        files: dict[str, bytes],
        run_files: _RunFiles,
        on_progress: ProgressCallback | None,
    ) -> tuple[bytes, Size]:
        try:
            async with ComfyClient(self.config) as client:
                try:
                    info = await client.object_info()
                except ComfyHTTPError as exc:
                    raise BackendUnavailable(self._erroring(exc)) from exc
                problem = check_route(info, self.config, route)
                if problem is not None:
                    raise BackendUnavailable(problem)
                for name, data in files.items():
                    await client.upload_image(name, data)
                    run_files.uploaded += 1
                prompt_id = await run_prompt(
                    client, graph, self.config, progress_node=graphs.SAMPLER, on_progress=on_progress
                )
                saved = _saved_image(await client.history(prompt_id), prompt_id)
                run_files.outputs = 1
                png = await client.view(saved["filename"], saved["subfolder"], saved["type"])
        except ComfyUnreachable as exc:
            raise _with_notes(BackendUnavailable(self._down(exc)), exc) from exc
        except ComfyHTTPError as exc:
            raise _with_notes(StepFailed(str(exc)), exc) from exc
        try:
            image = await asyncio.to_thread(imaging.decode, png, {"PNG"})
        except imaging.ImageError as exc:
            raise UnexpectedResponse(f"ComfyUI's output for prompt {prompt_id} is not a PNG", repr(png[:64])) from exc
        return png, imaging.size_of(image)

    def _clean_up_after(self, failure: BaseException, run_files: _RunFiles) -> None:
        """Remove the run's files; a problem doing so is noted on failure, which stays the error that is raised."""
        try:
            remove_run_files(self.config, run_files.token, uploaded=run_files.uploaded, outputs=run_files.outputs)
        except (CleanupError, OSError) as exc:
            failure.add_note(f"cleaning up this run's ComfyUI files also failed: {exc}")

    def _offline(self, problem: Unavailable) -> BackendStatus:
        return BackendStatus(self.name, self.config.host, False, None, {route: problem for route in Route})

    def _down(self, exc: ComfyUnreachable) -> Unavailable:
        return Unavailable(UnavailableCode.BACKEND_DOWN, f"ComfyUI 未运行或连不上（{self.config.host}）", str(exc))

    def _erroring(self, exc: ComfyHTTPError) -> Unavailable:
        return Unavailable(UnavailableCode.BACKEND_DOWN, f"ComfyUI 回应出错（HTTP {exc.status}）", str(exc))


def _version(stats: dict[str, Any]) -> str:
    try:
        return str(stats["system"]["comfyui_version"])
    except (KeyError, TypeError) as exc:
        raise UnexpectedResponse("GET /system_stats has no system.comfyui_version", str(stats)[:2000]) from exc


def _saved_image(history: dict[str, Any], prompt_id: str) -> dict[str, str]:
    """{"filename", "subfolder", "type"} of the step's one output image, from ComfyUI's history entry."""
    try:
        entry = history.get(prompt_id)
        if entry is None:
            raise UnexpectedResponse(
                f"ComfyUI finished prompt {prompt_id} but has no history for it", str(history)[:2000]
            )
        status = entry.get("status") or {}
        if status.get("status_str") != "success":
            raise StepFailed(f"ComfyUI status for prompt {prompt_id}: {status}")
        images = entry.get("outputs", {}).get(graphs.SAVE, {}).get("images", [])
        if len(images) != 1:
            raise UnexpectedResponse(f"expected 1 image from node {graphs.SAVE!r} of prompt {prompt_id}", str(images))
        image = images[0]
        if not all(isinstance(image.get(key), str) for key in ("filename", "subfolder", "type")):
            raise UnexpectedResponse(
                f"output image of prompt {prompt_id} lacks filename / subfolder / type", str(image)
            )
    except (AttributeError, TypeError) as exc:
        raise UnexpectedResponse(
            f"ComfyUI's history for prompt {prompt_id} has an unknown shape", str(history)[:2000]
        ) from exc
    return image


def _with_notes(target: BaseException, source: BaseException) -> BaseException:
    """target replaces source as the raised error; keep source's notes (e.g. a failed stop) visible on it."""
    for note in getattr(source, "__notes__", ()):
        target.add_note(note)
    return target
