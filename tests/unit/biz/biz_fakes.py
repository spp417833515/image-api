"""A scriptable in-memory ImageBackend and image helpers for the business-layer and web tests (no network)."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from pathlib import Path

from imagegen.biz.service import ImageGenService
from imagegen.logic.backend import BackendCapabilities, BackendStatus, Unavailable, UnavailableCode
from imagegen.logic.types import ProgressCallback, Route, StepInput, StepOutput
from PIL import Image, ImageDraw

VERSION = {"tag": "v0.42.5", "commit": "483dae4"}


@dataclass(frozen=True)
class FakeVersion:
    tag: str | None = VERSION["tag"]
    commit: str = VERSION["commit"]


class FakeBackend:
    """Every route available and every run succeeding, unless a test says otherwise.

    failures[i]: the exception the i-th run (0-based) raises. hold: while set, runs wait on it after reporting progress.
    """

    name = "fake"

    def __init__(
        self,
        routes: frozenset[Route] = frozenset(Route),
        max_images: int = 16,
        size_multiple: int = 32,
        max_seed: int = 2**64 - 1,
    ) -> None:
        self.caps = BackendCapabilities(routes, max_images, size_multiple, max_seed)
        self.online = True
        self.problems: dict[Route, Unavailable | None] = dict.fromkeys(routes)
        self.status_calls = 0
        self.runs: list[StepInput] = []
        self.failures: dict[int, BaseException] = {}
        self.hold: asyncio.Event | None = None
        self.started = asyncio.Event()

    def capabilities(self) -> BackendCapabilities:
        return self.caps

    async def status(self) -> BackendStatus:
        self.status_calls += 1
        if not self.online:
            down = Unavailable(UnavailableCode.BACKEND_DOWN, "后端没开（fake）", "connection refused (fake)")
            return BackendStatus(self.name, "fake:0", False, None, dict.fromkeys(self.problems, down))
        return BackendStatus(self.name, "fake:0", True, "1.0", dict(self.problems))

    async def run(self, step: StepInput, on_progress: ProgressCallback | None = None) -> StepOutput:
        index = len(self.runs)
        self.runs.append(step)
        if on_progress is not None:
            on_progress(0.5)
        self.started.set()
        if self.hold is not None:
            await self.hold.wait()
        if index in self.failures:
            raise self.failures[index]
        out = png(step.size.width, step.size.height, color=(index * 40 % 256, 90, 160))
        return StepOutput(out, step.size.width, step.size.height, step.seed)

    def go_down(self) -> None:
        self.online = False

    def lose_file(self, route: Route, path: str) -> None:
        self.problems[route] = Unavailable(UnavailableCode.MISSING_FILE, f"缺少蒙版权重 {path}", path)


def make_service(backend: FakeBackend, data_dir: Path) -> ImageGenService:
    return ImageGenService(backend, data_dir, FakeVersion)


def png(width: int, height: int, color: tuple[int, int, int] = (120, 140, 160)) -> bytes:
    return encode(Image.new("RGB", (width, height), color), "PNG")


def mask_png(width: int, height: int, box: tuple[int, int, int, int] | None = None, grey: int = 255) -> bytes:
    """Black, with box (left, top, right, bottom) painted in grey (255 = white). box=None: all black."""
    image = Image.new("L", (width, height), 0)
    if box is not None:
        ImageDraw.Draw(image).rectangle(box, fill=grey)
    return encode(image, "PNG")


def encode(image: Image.Image, fmt: str) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


def decoded(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.copy()
