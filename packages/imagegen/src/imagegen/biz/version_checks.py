"""The checks an upgrade / rollback runs on the version it has switched to: it starts, its nodes are complete, and each
route really makes a small image.

A check passes by returning; any exception is a failure, its text kept as the check's note (CheckFailed carries a note
written here, anything else its original text).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from ..logic import imaging
from ..logic.backend import BackendUnavailable, ImageBackend, StepFailed
from ..logic.types import Route, Size, StepInput, StepOutput
from .routing import ROUTE_NAMES

# The mask route's limit outside the white area: mean |difference| per pixel, 0–255, between the base image and the
# result (the user's limit, ppwiki decision id=5; measured 1.53 on a photo, up to 4.25 on line art).
MAX_MASK_OUTSIDE_DRIFT = 5.0

START_TIMEOUT_S = 180.0  # ComfyUI is online about 10 s after start; models load on the first image, not here
TRIAL_SIZE = Size(512, 512)
TRIAL_SEED = 1
TRIAL_T2I = "A red fox sitting in fresh snow, soft morning light."
TRIAL_REF = "Make the fox's fur white, keep everything else the same."
TRIAL_MASK = "A white arctic fox sitting in fresh snow."
_MASK_BOX = (160, 160, 352, 352)  # the middle of the image


class CheckFailed(Exception):
    """A check did not pass; the message says what was found and what was expected."""


class Check(Protocol):
    name: ClassVar[str]  # stable key, e.g. "routes"
    label: ClassVar[str]  # shown to people, e.g. "三条路试跑"

    async def run(self) -> None: ...


@dataclass(frozen=True, slots=True)
class StartsCheck:
    """ComfyUI answers within timeout_s of being started."""

    wait_online: Callable[[float], Awaitable[None]]
    timeout_s: float = START_TIMEOUT_S
    name: ClassVar[str] = "starts"
    label: ClassVar[str] = "能启动"

    async def run(self) -> None:
        await self.wait_online(self.timeout_s)


@dataclass(frozen=True, slots=True)
class NodesCheck:
    """The backend's status has every route it declares available (nodes, node inputs and model files present)."""

    backend: ImageBackend
    name: ClassVar[str] = "nodes"
    label: ClassVar[str] = "节点齐全"

    async def run(self) -> None:
        status = await self.backend.status()
        problems = []
        for route in sorted(self.backend.capabilities().routes):
            if route not in status.routes:
                problems.append(f"{ROUTE_NAMES[route]}：状态里没有这条路")
            elif (problem := status.routes[route]) is not None:
                problems.append(f"{ROUTE_NAMES[route]}：{problem.message}（{problem.raw}）")
        if problems:
            raise CheckFailed("\n".join(problems))


@dataclass(frozen=True, slots=True)
class RoutesCheck:
    """Each route makes one TRIAL_SIZE image. The text-to-image result is the base of the other two (a natural image:
    on flat synthetic colours the mask route shifts colours everywhere, 9.2/255 measured, so the limit would not
    hold); the mask route must also keep the area outside its mask within MAX_MASK_OUTSIDE_DRIFT."""

    backend: ImageBackend
    name: ClassVar[str] = "routes"
    label: ClassVar[str] = "三条路试跑"

    async def run(self) -> None:
        failures = []
        try:
            base = await self._run(StepInput(Route.T2I, TRIAL_T2I, (), None, TRIAL_SEED, TRIAL_SIZE))
        except Exception as exc:
            failures.append(f"{ROUTE_NAMES[Route.T2I]}：{_original_text(exc)}")
            failures += [f"{ROUTE_NAMES[r]}：没有运行（它用文生图的结果做底图）" for r in (Route.REF, Route.MASK)]
            raise CheckFailed("\n".join(failures)) from exc
        for route, trial in ((Route.REF, self._reference_edit), (Route.MASK, self._mask_rewrite)):
            try:  # both run, so the note lists every route that failed
                await trial(base.png)
            except Exception as exc:
                failures.append(f"{ROUTE_NAMES[route]}：{_original_text(exc)}")
        if failures:
            raise CheckFailed("\n".join(failures))

    async def _reference_edit(self, base: bytes) -> None:
        await self._run(StepInput(Route.REF, TRIAL_REF, (base,), None, TRIAL_SEED, TRIAL_SIZE))

    async def _mask_rewrite(self, base: bytes) -> None:
        mask = trial_mask()
        output = await self._run(
            StepInput(Route.MASK, TRIAL_MASK, (base,), imaging.to_png(mask), TRIAL_SEED, TRIAL_SIZE)
        )
        before, after = imaging.decode(base), imaging.decode(output.png, {"PNG"})
        _, outside = await asyncio.to_thread(mask_drift, before, after, mask)
        if outside > MAX_MASK_OUTSIDE_DRIFT:
            raise CheckFailed(f"白区外平均差 {outside:.2f}/255，上限 {MAX_MASK_OUTSIDE_DRIFT}/255")

    async def _run(self, step: StepInput) -> StepOutput:
        output = await self.backend.run(step)
        if (output.width, output.height) != (step.size.width, step.size.height):
            raise CheckFailed(f"出图 {output.width}x{output.height}，应为 {step.size}")
        return output


def default_checks(wait_online: Callable[[float], Awaitable[None]], backend: ImageBackend) -> Sequence[Check]:
    """In the order they run: a later check means nothing once an earlier one failed."""
    return (StartsCheck(wait_online), NodesCheck(backend), RoutesCheck(backend))


def mask_drift(base: Image.Image, result: Image.Image, mask: Image.Image) -> tuple[float, float]:
    """Mean |difference| (0–255, luminance of the per-channel difference) inside and outside the white area."""
    diff = ImageChops.difference(base.convert("RGB"), result.convert("RGB")).convert("L")
    inside = ImageStat.Stat(diff, mask=mask).mean[0]
    outside = ImageStat.Stat(diff, mask=ImageOps.invert(mask)).mean[0]
    return inside, outside


def trial_mask() -> Image.Image:
    """The mask of the mask trial: white = repaint, mode L, TRIAL_SIZE."""
    mask = Image.new("L", (TRIAL_SIZE.width, TRIAL_SIZE.height), 0)
    ImageDraw.Draw(mask).rectangle(_MASK_BOX, fill=255)
    return mask


def _original_text(exc: Exception) -> str:
    """What a failed trial shows: the backend's original words, plus any notes attached to them."""
    match exc:
        case CheckFailed():
            text = str(exc)
        case StepFailed():
            text = exc.raw
        case BackendUnavailable():
            text = f"{exc.message}：{exc.raw}"
        case _:
            text = f"{type(exc).__name__}: {exc}"
    return "\n".join([text, *getattr(exc, "__notes__", ())])
