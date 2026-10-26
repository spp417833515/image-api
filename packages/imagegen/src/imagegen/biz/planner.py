"""Planning: for each step its route, predicted output size, or the reason it is rejected; plus route availability.

Planning reads only local image metadata, the masks, and the availability it is given: it never waits on the backend
(the page re-plans 250 ms after every edit). Checks run in a fixed order and the first that fails is the step's
reject, so a step carries at most one reason; the instruction is checked last and never affects the route.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image

from ..logic import imaging
from ..logic.storage import ImageNotFound, ImageStore
from ..logic.types import Route, Size
from .availability import RouteProblem
from .models import GenerateRequest, ImageSize, PlanResponse, PlanStep, Reject, RejectCode, StepRequest, UnavailableInfo
from .routing import MASK_THRESHOLD, MAX_IMAGES, PREV, ROUTE_NAMES, Limits, route_for

_MASK_CACHE = 256  # masks are immutable once stored, so their facts are cached by id


@dataclass(frozen=True, slots=True)
class StepPlan:
    number: int
    route: Route | None  # None exactly when rejected
    output: Size | None  # predicted output size; known for every step that got as far as sizing
    requested: Size | None  # the t2i size asked for, when rounding changed it
    reject: Reject | None
    unavailable: RouteProblem | None

    def to_response(self) -> PlanStep:
        rejected = self.reject is not None
        return PlanStep(
            step=self.number,
            route=self.route,
            output=None if rejected or self.output is None else _size(self.output),
            requested=None if rejected or self.requested is None else _size(self.requested),
            reject=self.reject,
            unavailable=None
            if self.unavailable is None
            else UnavailableInfo(code=self.unavailable.code, message=self.unavailable.message),
        )


def plan_response(plans: list[StepPlan]) -> PlanResponse:
    steps = [p.to_response() for p in plans]
    return PlanResponse(ok=all(s.reject is None and s.unavailable is None for s in steps), steps=steps)


class _Rejected(Exception):
    def __init__(self, code: RejectCode, message: str) -> None:
        super().__init__(message)
        self.reject = Reject(code=code, message=message)


class Planner:
    def __init__(self, store: ImageStore, limits: Limits) -> None:
        self._store = store
        self._limits = limits
        self._mask_facts = lru_cache(maxsize=_MASK_CACHE)(self._inspect_mask)

    def plan(self, request: GenerateRequest, problems: Mapping[Route, RouteProblem | None]) -> list[StepPlan]:
        """problems: availability per route the backend supports (see Availability.problems)."""
        plans: list[StepPlan] = []
        for index, step in enumerate(request.steps):
            number = request.first_step + index
            previous = plans[-1].output if plans else None
            output = None
            try:
                route, base, output, requested = self._route_and_size(step, index, previous)
                self._check_details(step, route, base, output)
            except _Rejected as rejected:
                plans.append(StepPlan(number, None, output, None, rejected.reject, None))
                continue
            plans.append(StepPlan(number, route, output, requested, None, problems[route]))
        return plans

    def _route_and_size(
        self, step: StepRequest, index: int, previous: Size | None
    ) -> tuple[Route, Size | None, Size | None, Size | None]:
        """(route, first image size, output size, requested size), or the structural reason the step cannot be routed.

        Sizes are None when unknown: the first image is "$prev" and the previous step has no predicted output, or a
        t2i size is out of range.
        """
        count = len(step.images)
        if count > MAX_IMAGES:
            raise _Rejected(
                RejectCode.TOO_MANY_IMAGES,
                f"这一步有 {count} 张图，最多 {MAX_IMAGES} 张，请去掉 {count - MAX_IMAGES} 张",
            )
        if index == 0 and PREV in step.images:
            raise _Rejected(RejectCode.PREV_ON_FIRST_STEP, "这批的第 1 步没有上一步可接，请换成具体的图")
        sizes = {image_id: self._stored_size(image_id) for image_id in step.images if image_id != PREV}
        if step.mask is not None:
            self._stored_size(step.mask)
        route = route_for(count, step.mask is not None)
        if route is None:
            raise _Rejected(RejectCode.MASK_WITHOUT_IMAGE, "有蒙版但没有底图：加一张图，或去掉蒙版")
        if count and (step.width is not None or step.height is not None):
            raise _Rejected(RejectCode.SIZE_WITH_IMAGES, "带图的步骤尺寸跟第 1 张图走，请清掉宽高")
        if route not in self._limits.routes:
            raise _Rejected(RejectCode.ROUTE_UNSUPPORTED, f"当前生图后端不支持{ROUTE_NAMES[route]}")
        if count > self._limits.max_images:
            raise _Rejected(
                RejectCode.TOO_MANY_IMAGES, f"当前生图后端每步最多 {self._limits.max_images} 张图，这一步有 {count} 张"
            )
        if route is Route.T2I:
            return route, None, *self._text_to_image_size(step)
        base = previous if step.images[0] == PREV else sizes[step.images[0]]
        return route, base, None if base is None else self._limits.edit_size(base), None

    def _check_details(self, step: StepRequest, route: Route, base: Size | None, output: Size | None) -> None:
        if route is Route.MASK:
            assert step.mask is not None
            mask_size, empty = self._mask_facts(step.mask)
            if base is not None and mask_size != base:
                raise _Rejected(
                    RejectCode.MASK_SIZE_MISMATCH,
                    f"新图尺寸变了，蒙版要重新涂（蒙版 {_shown(mask_size)}，底图 {_shown(base)}）",
                )
            if empty:
                raise _Rejected(RejectCode.MASK_EMPTY, "蒙版是空的：在要重画的地方涂白")
        if route is Route.T2I and output is None:
            width, height = self._requested(step)
            raise _Rejected(
                RejectCode.SIZE_OUT_OF_RANGE,
                f"宽高取整到 {self._limits.size_multiple} 的倍数后每边要在 {self._limits.min_edge}–{self._limits.max_edge} 之间，"
                f"现在是 {width}×{height}",
            )
        if step.seed is not None and not 0 <= step.seed <= self._limits.max_seed:
            raise _Rejected(RejectCode.SEED_OUT_OF_RANGE, f"seed 要在 0 到 {self._limits.max_seed} 之间")
        if not step.instruction.strip():
            raise _Rejected(RejectCode.EMPTY_INSTRUCTION, "写一句指令：要画什么，或要怎么改")

    def _text_to_image_size(self, step: StepRequest) -> tuple[Size | None, Size | None]:
        """(output, requested): output None when out of range; requested only when the user's size was rounded."""
        width, height = self._requested(step)
        output = self._limits.text_to_image_size(width, height)
        if output is None or (output.width, output.height) == (width, height):
            return output, None
        return output, Size(width, height)

    def _requested(self, step: StepRequest) -> tuple[int, int]:
        default = self._limits.default_size
        return (
            default.width if step.width is None else step.width,
            default.height if step.height is None else step.height,
        )

    def _stored_size(self, image_id: str) -> Size:
        try:
            meta = self._store.meta(image_id)
        except ImageNotFound:
            raise _Rejected(RejectCode.UNKNOWN_IMAGE, f"找不到图片 {image_id}，请重新上传") from None
        return Size(meta.width, meta.height)

    def _inspect_mask(self, image_id: str) -> tuple[Size, bool]:
        mask = binarized_mask(self._store, image_id)
        return imaging.size_of(mask), imaging.is_all_black(mask)


def binarized_mask(store: ImageStore, image_id: str) -> Image.Image:
    """The stored mask as the backend gets it: white (luminance ≥ MASK_THRESHOLD) = repaint."""
    return imaging.binarize_mask(imaging.decode(store.path(image_id)), MASK_THRESHOLD)


def _shown(size: Size) -> str:
    return f"{size.width}×{size.height}"


def _size(size: Size) -> ImageSize:
    return ImageSize(width=size.width, height=size.height)
