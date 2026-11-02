"""The HTTP contract v1 as pydantic models: the single source of every request and response shape.

The web layer serialises these directly. GET /api/status's models are in status_models.py. Invariants the page relies on (exactly one of route / reject, step numbering,
a failed job has a failed step with its error, ...) are enforced here, so no response can break them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ..logic.backend import UnavailableCode
from ..logic.storage import ImageMeta
from ..logic.types import ModelId, Route

IMAGES_PATH = "/api/images"

_MODEL = ConfigDict(frozen=True, extra="forbid")


def image_url(image_id: str) -> str:
    return f"{IMAGES_PATH}/{image_id}"


class RejectCode(StrEnum):
    EMPTY_INSTRUCTION = "empty_instruction"
    TOO_MANY_IMAGES = "too_many_images"
    MASK_WITHOUT_IMAGE = "mask_without_image"
    MASK_EMPTY = "mask_empty"
    MASK_SIZE_MISMATCH = "mask_size_mismatch"
    SIZE_WITH_IMAGES = "size_with_images"
    PREV_ON_FIRST_STEP = "prev_on_first_step"
    UNKNOWN_IMAGE = "unknown_image"
    ROUTE_UNSUPPORTED = "route_unsupported"
    SIZE_OUT_OF_RANGE = "size_out_of_range"
    SEED_OUT_OF_RANGE = "seed_out_of_range"


class UnavailableReason(StrEnum):
    """The backend's reasons plus the service's own: an upgrade is running."""

    COMFY_DOWN = UnavailableCode.BACKEND_DOWN.value
    MISSING_FILE = UnavailableCode.MISSING_FILE.value
    MISSING_NODE = UnavailableCode.MISSING_NODE.value
    UPGRADING = "upgrading"


class ErrorCode(StrEnum):
    """The code of every error body the service sends: its single source (web and biz import it)."""

    REJECTED = "rejected"  # 422 with the plan
    BAD_IMAGE = "bad_image"  # 422: an upload that is not a PNG / JPEG / WEBP image
    INVALID_REQUEST = "invalid_request"  # 422: the request does not fit the contract
    NOT_FOUND = "not_found"  # 404: unknown path, image or job
    METHOD_NOT_ALLOWED = "method_not_allowed"  # 405
    INTERNAL_ERROR = "internal_error"  # 500: a bug; raw has what happened
    # 503: a route the request needs cannot run now
    COMFY_DOWN = UnavailableReason.COMFY_DOWN.value
    MISSING_FILE = UnavailableReason.MISSING_FILE.value
    MISSING_NODE = UnavailableReason.MISSING_NODE.value
    UPGRADING = UnavailableReason.UPGRADING.value


# ---------------------------------------------------------------- request (POST /api/plan and /api/generate)
class StepRequest(BaseModel):
    model_config = _MODEL

    instruction: str
    images: list[str] = []  # image ids or "$prev"; any number (more than allowed is a reject, not a format error)
    mask: str | None = None
    seed: int | None = None  # None: random, reported back
    width: int | None = None  # text-to-image only
    height: int | None = None


class GenerateRequest(BaseModel):
    model_config = _MODEL

    model: ModelId = ModelId.QWEN_IMAGE_2_1  # which weights every step of this request runs
    first_step: int = Field(default=1, ge=1)  # the page's number for steps[0]; plans, jobs and errors count from it
    steps: list[StepRequest] = Field(min_length=1)


# ---------------------------------------------------------------- images
class ImageSize(BaseModel):
    model_config = _MODEL

    width: int
    height: int


class UploadedImage(BaseModel):
    model_config = _MODEL

    id: str
    url: str
    width: int
    height: int

    @classmethod
    def of(cls, meta: ImageMeta) -> UploadedImage:
        return cls(id=meta.id, url=image_url(meta.id), width=meta.width, height=meta.height)


# ---------------------------------------------------------------- plan
class Reject(BaseModel):
    model_config = _MODEL

    code: RejectCode
    message: str = Field(min_length=1)  # one plain sentence, shown verbatim


class UnavailableInfo(BaseModel):
    model_config = _MODEL

    code: UnavailableReason
    message: str = Field(min_length=1)


class PlanStep(BaseModel):
    model_config = _MODEL

    step: int
    route: Route | None
    output: ImageSize | None
    requested: ImageSize | None
    reject: Reject | None
    unavailable: UnavailableInfo | None

    @model_validator(mode="after")
    def _route_or_reject(self) -> Self:
        if (self.route is None) == (self.reject is None):
            raise ValueError(
                f"step {self.step}: exactly one of route and reject must be set, got {self.route=} {self.reject=}"
            )
        return self


class PlanResponse(BaseModel):
    model_config = _MODEL

    ok: bool  # no step is rejected and every step's route is available
    steps: list[PlanStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _require_consecutive(self.steps[0].step, [s.step for s in self.steps])
        runnable = all(s.reject is None and s.unavailable is None for s in self.steps)
        if self.ok != runnable:
            raise ValueError(f"ok={self.ok} contradicts the steps (runnable={runnable})")
        return self


# ---------------------------------------------------------------- jobs
class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class StepState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # an earlier step failed


class GenerateAccepted(BaseModel):
    model_config = _MODEL

    job_id: str = Field(min_length=1)


class JobImage(BaseModel):
    model_config = _MODEL

    id: str
    url: str
    width: int
    height: int
    route: Route
    seed: int

    @classmethod
    def of(cls, meta: ImageMeta) -> JobImage:
        if meta.route is None or meta.seed is None:
            raise ValueError(f"image {meta.id} is not a generated image (route={meta.route}, seed={meta.seed})")
        return cls(
            id=meta.id, url=image_url(meta.id), width=meta.width, height=meta.height, route=meta.route, seed=meta.seed
        )


class StepError(BaseModel):
    model_config = _MODEL

    raw: str = Field(min_length=1)  # the backend's original text


class JobStep(BaseModel):
    model_config = _MODEL

    step: int
    route: Route
    state: StepState
    progress: float | None = Field(ge=0.0, le=1.0)  # running steps only
    image: JobImage | None
    error: StepError | None

    @model_validator(mode="after")
    def _state_fits(self) -> Self:
        problems = []
        if (self.state is StepState.DONE) != (self.image is not None):
            problems.append(f"image={self.image is not None}")
        if (self.state is StepState.FAILED) != (self.error is not None):
            problems.append(f"error={self.error is not None}")
        if self.progress is not None and self.state is not StepState.RUNNING:
            problems.append(f"progress={self.progress}")
        if problems:
            raise ValueError(f"step {self.step} in state {self.state} cannot have " + ", ".join(problems))
        return self


class Job(BaseModel):
    model_config = _MODEL

    id: str
    state: JobState
    first_step: int
    steps: list[JobStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _require_consecutive(self.first_step, [s.step for s in self.steps])
        states = {s.state for s in self.steps}
        expected = {
            JobState.QUEUED: states == {StepState.QUEUED},
            JobState.RUNNING: StepState.FAILED not in states and states != {StepState.DONE},
            JobState.DONE: states == {StepState.DONE},
            JobState.FAILED: StepState.FAILED in states
            and states <= {StepState.DONE, StepState.FAILED, StepState.SKIPPED},
        }
        if not expected[self.state]:
            raise ValueError(f"job {self.id} is {self.state} but its steps are {[s.state.value for s in self.steps]}")
        return self


# ---------------------------------------------------------------- history
class HistoryItem(BaseModel):
    model_config = _MODEL

    id: str
    url: str
    width: int
    height: int
    route: Route
    seed: int
    instruction: str
    created_at: AwareDatetime
    job_id: str
    step: int

    @classmethod
    def of(cls, meta: ImageMeta) -> HistoryItem:
        return cls.model_validate({**meta.model_dump(exclude={"kind"}), "url": image_url(meta.id)})


class HistoryPage(BaseModel):
    model_config = _MODEL

    items: list[HistoryItem]  # newest first


# ---------------------------------------------------------------- error bodies
class ErrorBody(BaseModel):
    model_config = _MODEL

    code: ErrorCode
    message: str = Field(min_length=1)
    raw: str | None = None


class RejectedBody(ErrorBody):
    code: Literal[ErrorCode.REJECTED] = ErrorCode.REJECTED
    plan: PlanResponse


def _require_consecutive(first: int, numbers: list[int]) -> None:
    expected = list(range(first, first + len(numbers)))
    if numbers != expected:
        raise ValueError(f"step numbers must be {expected}, got {numbers}")
