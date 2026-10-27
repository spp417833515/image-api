"""Jobs: accepted requests run in the background, one step at a time on one worker (the GPU is strictly serial).

Steps run in order; "$prev" is the previous step's output. The first failure stops the job: that step is failed with
the backend's original text, later steps are skipped, earlier images stay. Each finished image is stored (and so is in
the history) before its step is reported done. A mask step's result is the original with only the white area
repainted (masking.paste_back). A step does not start while a ComfyUI upgrade is running. Jobs live in
memory: a restart forgets them (their images remain).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field

from ..logic import imaging
from ..logic.backend import BackendUnavailable, CleanupFailed, ImageBackend, StepFailed, UnexpectedResponse
from ..logic.storage import ImageKind, ImageMeta, ImageStore
from ..logic.types import Route, Size, StepInput
from .masking import binarized_mask, paste_back
from .models import Job, JobImage, JobState, JobStep, StepError, StepState
from .routing import PREV, Limits
from .version_state import RECOVER_COMMAND, Interrupted, Upgrading, VersionState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunStep:
    """One planned, accepted step."""

    number: int
    route: Route
    instruction: str
    images: tuple[str, ...]  # image ids, or PREV
    mask: str | None
    seed: int
    size: Size | None  # t2i output size; ref / mask follow their first image, measured when the step runs


@dataclass(slots=True)
class _StepRun:
    plan: RunStep
    state: StepState = StepState.QUEUED
    progress: float | None = None
    image: ImageMeta | None = None
    error: str | None = None


@dataclass(slots=True)
class _JobRun:
    id: str
    first_step: int
    steps: list[_StepRun]
    state: JobState = JobState.QUEUED
    finished: asyncio.Event = field(default_factory=asyncio.Event)


class _NotRun(Exception):
    """The step was not sent to the backend; the message says why."""


class JobNotFound(KeyError):
    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self.job_id = job_id


class JobRunner:
    def __init__(
        self,
        backend: ImageBackend,
        store: ImageStore,
        limits: Limits,
        version_state: VersionState,
        on_unavailable: Callable[[], None],
    ) -> None:
        """version_state: read before every step, so no step starts during an upgrade or after an interrupted one.
        on_unavailable: called when the backend turns out to be unavailable mid-job (to refresh the status)."""
        self._backend = backend
        self._store = store
        self._limits = limits
        self._version_state = version_state
        self._on_unavailable = on_unavailable
        self._jobs: dict[str, _JobRun] = {}
        self._queue: asyncio.Queue[_JobRun] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    @property
    def started(self) -> bool:
        return self._worker is not None

    async def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("the job runner is already started")
        self._worker = asyncio.create_task(self._work(), name="imagegen-jobs")

    async def stop(self) -> None:
        """Stop the worker. A step that is running is interrupted (the backend stops its work); unfinished jobs end
        failed, so nobody waits on them forever."""
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.cancel()
        await asyncio.wait({worker})  # waits for the worker to wind down without re-raising its cancellation
        for job in self._jobs.values():
            if not job.finished.is_set():
                _abort(job, "服务已停止，这个任务没有跑完")

    def submit(self, first_step: int, steps: list[RunStep]) -> str:
        if self._worker is None:
            raise RuntimeError("the job runner is not started: use `async with service:` or `await service.start()`")
        job = _JobRun(f"job_{time.time_ns():016x}{secrets.token_hex(3)}", first_step, [_StepRun(s) for s in steps])
        self._jobs[job.id] = job
        self._queue.put_nowait(job)
        return job.id

    def view(self, job_id: str) -> Job:
        job = self._get(job_id)
        return Job(
            id=job.id,
            state=job.state,
            first_step=job.first_step,
            steps=[
                JobStep(
                    step=run.plan.number,
                    route=run.plan.route,
                    state=run.state,
                    progress=run.progress,
                    image=None if run.image is None else JobImage.of(run.image),
                    error=None if run.error is None else StepError(raw=run.error),
                )
                for run in job.steps
            ],
        )

    async def wait(self, job_id: str) -> Job:
        await self._get(job_id).finished.wait()
        return self.view(job_id)

    def _get(self, job_id: str) -> _JobRun:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise JobNotFound(job_id) from None

    async def _work(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._run(job)
            except Exception:
                # _run turns every step's failure into that step's error; reaching here is a bug in the runner itself.
                # The job ends failed with the traceback, and the worker goes on with the next job.
                logger.exception("job %s: the job runner failed", job.id)
                _abort(job, f"任务运行器内部错误，这个任务停下了：\n{traceback.format_exc()}")

    async def _run(self, job: _JobRun) -> None:
        job.state = JobState.RUNNING
        previous: ImageMeta | None = None
        for run in job.steps:
            if job.state is JobState.FAILED:
                run.state = StepState.SKIPPED
                continue
            run.state, run.progress = StepState.RUNNING, 0.0
            try:
                if (block := self._version_state.generation_block()) is not None:
                    raise _NotRun(_not_run_because(block))
                previous = await self._run_step(job, run.plan, previous, _progress_setter(run))
            except Exception as exc:
                run.state, run.progress, run.error = StepState.FAILED, None, self._raw_error(job, run.plan, exc)
                job.state = JobState.FAILED
                continue
            run.state, run.progress, run.image = StepState.DONE, None, previous
        if job.state is not JobState.FAILED:
            job.state = JobState.DONE
        job.finished.set()

    async def _run_step(
        self, job: _JobRun, step: RunStep, previous: ImageMeta | None, on_progress: Callable[[float], None]
    ) -> ImageMeta:
        def stored(image_id: str) -> ImageMeta:
            if image_id != PREV:
                return self._store.meta(image_id)
            if previous is None:
                raise RuntimeError(f"step {step.number} uses $prev but no earlier step of job {job.id} has an image")
            return previous

        metas = [stored(image_id) for image_id in step.images]
        if step.route is Route.T2I:
            if step.size is None:
                raise RuntimeError(f"text-to-image step {step.number} of job {job.id} has no planned size")
            size = step.size
        else:
            size = self._limits.edit_size(Size(metas[0].width, metas[0].height))
        mask = None
        if (mask_id := step.mask) is not None:
            mask = await asyncio.to_thread(binarized_mask, self._store, mask_id)
        mask_png = None if mask is None else await asyncio.to_thread(imaging.to_png, mask)
        images = tuple(self._store.path(meta.id) for meta in metas)
        try:
            output = await self._backend.run(
                StepInput(step.route, step.instruction, images, mask_png, step.seed, size), on_progress
            )
        except CleanupFailed as exc:
            # The image is good; only the backend's temporary files stayed behind. Keep the image, report the leftovers.
            logger.error("job %s step %s: %s", job.id, step.number, exc)
            output = exc.output
        png = output.png
        if step.route is Route.MASK:
            assert mask is not None  # the planner rejects a mask step without a mask
            png = await asyncio.to_thread(paste_back, images[0], mask, output.png)
        return await asyncio.to_thread(
            self._store.save,
            png,
            kind=ImageKind.OUTPUT,
            route=step.route,
            seed=output.seed,
            instruction=step.instruction,
            job_id=job.id,
            step=step.number,
        )

    def _raw_error(self, job: _JobRun, step: RunStep, exc: Exception) -> str:
        """The text a failed step shows: the backend's original words, unedited, plus any notes attached to them."""
        match exc:
            case _NotRun():
                raw = str(exc)
            case StepFailed():
                raw = exc.raw
            case BackendUnavailable():
                raw = exc.raw
                self._on_unavailable()
            case UnexpectedResponse():
                logger.error(
                    "job %s step %s: the backend answered in an unknown form", job.id, step.number, exc_info=exc
                )
                raw = str(exc)
            case _:
                # Planning should have rejected anything that fails here: this is a bug, so log it in full.
                logger.error("job %s step %s failed unexpectedly", job.id, step.number, exc_info=exc)
                raw = f"内部错误 {type(exc).__name__}: {exc}"
        return "\n".join([raw or f"{type(exc).__name__}（没有错误原文）", *getattr(exc, "__notes__", ())])


def _not_run_because(block: Upgrading | Interrupted) -> str:
    match block:
        case Upgrading(target=target):
            return f"ComfyUI 正在升级（目标 {target}），这一步没有运行"
        case Interrupted(target=target):
            return f"上次升级到 {target} 中断了，这一步没有运行：运行 {RECOVER_COMMAND} 恢复"


def _progress_setter(run: _StepRun) -> Callable[[float], None]:
    def set_progress(value: float) -> None:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"the backend reported progress {value}, expected a value in [0, 1]")
        run.progress = value

    return set_progress


def _abort(job: _JobRun, raw: str) -> None:
    """End a job that cannot finish: its current (or next) step fails with raw, the rest are skipped."""
    failed = False
    for run in job.steps:
        if run.state in (StepState.RUNNING, StepState.QUEUED):
            if failed:
                run.state = StepState.SKIPPED
            else:
                run.state, run.progress, run.error, failed = StepState.FAILED, None, raw, True
    job.state = JobState.FAILED if failed or job.state is JobState.FAILED else JobState.DONE
    job.finished.set()
