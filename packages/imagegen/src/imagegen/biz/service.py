"""ImageGenService: the one entry point to image generation, for the web layer, programs and other projects.

    async with create_service(Path("data")) as service:      # imagegen.factory
        uploaded = await service.upload_image(png_bytes)
        accepted = await service.generate(GenerateRequest(steps=[StepRequest(instruction="a red fox in snow")]))
        job = await service.wait(accepted.job_id)
        png = service.image_bytes(job.steps[0].image.id)

Refusals are exceptions from imagegen.biz.errors (Rejected, ServiceUnavailable, BadImage, NotFound, InvalidRequest),
each carrying the contract's error code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Self

from ..logic import imaging
from ..logic.backend import ImageBackend
from ..logic.storage import ImageKind, ImageMeta, ImageNotFound, ImageStore, is_image_id
from ..logic.types import Route
from .availability import Availability, VersionReader
from .errors import BadImage, InvalidRequest, NotFound, Rejected, ServiceUnavailable
from .jobs import JobNotFound, JobRunner, RunStep
from .models import (
    GenerateAccepted,
    GenerateRequest,
    HistoryItem,
    HistoryPage,
    Job,
    PlanResponse,
    UploadedImage,
)
from .planner import Planner, plan_response
from .routing import Limits
from .status_models import StatusResponse
from .version_state import VersionState

UPLOAD_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
HISTORY_MAX_LIMIT = 200
# Pillow modes PNG cannot hold but that are ordinary colour photos (e.g. CMYK JPEGs): stored as RGB.
_COLOUR_MODES_TO_RGB = frozenset({"CMYK", "YCbCr", "LAB", "HSV"})


class ImageGenService:
    def __init__(self, backend: ImageBackend, data_dir: Path, read_version: VersionReader) -> None:
        """data_dir holds images/ (uploads and outputs) and comfy/ (version state). read_version returns the installed
        backend version as an object with .tag and .commit. Raises ValueError if the product limits do not fit the
        backend's capabilities."""
        self.backend = backend
        self.limits = Limits.for_backend(backend.capabilities())
        self.store = ImageStore(data_dir / "images")
        self.version_state = VersionState.in_data_dir(data_dir)
        self._availability = Availability(backend, read_version, self.version_state, self.limits)
        self._planner = Planner(self.store, self.limits)
        self._jobs = JobRunner(backend, self.store, self.limits, self.version_state, self._availability.refresh_soon)

    # ---- lifecycle: the job worker runs between start() and stop()
    async def start(self) -> None:
        await self._jobs.start()

    async def stop(self) -> None:
        await self._jobs.stop()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stop()

    # ---- images
    async def upload_image(self, data: bytes) -> UploadedImage:
        """Store a PNG / JPEG / WEBP image (as PNG, EXIF orientation applied). Raises BadImage."""
        png = await asyncio.to_thread(_upload_png, data)
        meta = await asyncio.to_thread(self.store.save, png, kind=ImageKind.UPLOAD)
        return UploadedImage.of(meta)

    def image_path(self, image_id: str) -> Path:
        """The stored PNG. Raises NotFound."""
        try:
            return self.store.path(image_id)
        except ImageNotFound as exc:
            raise NotFound(f"找不到图片 {image_id}") from exc

    def image_bytes(self, image_id: str) -> bytes:
        return self.image_path(image_id).read_bytes()

    def image_meta(self, image_id: str) -> ImageMeta:
        try:
            return self.store.meta(image_id)
        except ImageNotFound as exc:
            raise NotFound(f"找不到图片 {image_id}") from exc

    async def history(self, limit: int = 50, before: str | None = None) -> HistoryPage:
        """Generated images, newest first; before = the last id of the previous page (exclusive)."""
        if not 1 <= limit <= HISTORY_MAX_LIMIT:
            raise InvalidRequest(f"limit 要在 1 到 {HISTORY_MAX_LIMIT} 之间，收到 {limit}")
        if before is not None and not is_image_id(before):
            raise InvalidRequest(f"before 要是图片 id（img_ 加 22 位十六进制），收到 {before!r}")
        metas = await asyncio.to_thread(self.store.history, limit=limit, before=before)
        return HistoryPage(items=[HistoryItem.of(meta) for meta in metas])

    # ---- planning and generation
    async def plan(self, request: GenerateRequest) -> PlanResponse:
        """Route, output size or reject per step, and availability. Never waits on the backend (cached status)."""
        snapshot = await self._availability.cached()
        problems = self._availability.problems(snapshot, self.version_state.generation_block())
        return plan_response(await asyncio.to_thread(self._planner.plan, request, problems))

    async def generate(self, request: GenerateRequest) -> GenerateAccepted:
        """Plan with the backend's current status and queue the job. Raises Rejected (some step is rejected) or
        ServiceUnavailable (a route it needs cannot run now; the cached status is updated, so status() and plan()
        show it at once)."""
        if not self._jobs.started:
            raise RuntimeError("the service is not started: use `async with service:` or `await service.start()`")
        block = self.version_state.generation_block()
        snapshot = await (self._availability.cached() if block else self._availability.refresh())
        plans = await asyncio.to_thread(self._planner.plan, request, self._availability.problems(snapshot, block))
        if any(p.reject is not None for p in plans):
            raise Rejected(plan_response(plans))
        blocked = next((p.unavailable for p in plans if p.unavailable is not None), None)
        if blocked is not None:
            raise ServiceUnavailable(blocked.code, blocked.message, blocked.raw)
        steps = []
        for plan, step in zip(plans, request.steps, strict=True):
            assert plan.route is not None
            steps.append(
                RunStep(
                    number=plan.number,
                    route=plan.route,
                    instruction=step.instruction,
                    images=tuple(step.images),
                    mask=step.mask,
                    seed=self.limits.random_seed() if step.seed is None else step.seed,
                    size=plan.output if plan.route is Route.T2I else None,
                )
            )
        return GenerateAccepted(job_id=self._jobs.submit(request.first_step, steps))

    def job(self, job_id: str) -> Job:
        try:
            return self._jobs.view(job_id)
        except JobNotFound as exc:
            raise NotFound(f"找不到任务 {job_id}（服务重启后，之前的任务就查不到了，图在历史里）") from exc

    async def wait(self, job_id: str) -> Job:
        """The job once it is done or failed."""
        try:
            return await self._jobs.wait(job_id)
        except JobNotFound as exc:
            raise NotFound(f"找不到任务 {job_id}") from exc

    # ---- status
    async def status(self) -> StatusResponse:
        return await self._availability.status()


def _upload_png(data: bytes) -> bytes:
    try:
        image = imaging.decode(data, UPLOAD_FORMATS)
    except imaging.ImageError as exc:
        raise BadImage("只能用 PNG、JPG 或 WEBP 图片，这个文件读不出来", raw=str(exc)) from exc
    if image.mode in _COLOUR_MODES_TO_RGB:
        image = image.convert("RGB")
    try:
        return imaging.to_png(image)
    except (OSError, ValueError) as exc:
        raise BadImage(f"这张图（{image.mode} 模式）无法保存", raw=str(exc)) from exc
