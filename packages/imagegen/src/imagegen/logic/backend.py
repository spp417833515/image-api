"""The image-generation backend interface: the extension point for plugging in other image APIs.

The business layer depends only on this module; concrete backends (e.g. imagegen.logic.comfy) implement ImageBackend.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .types import ProgressCallback, Route, StepInput, StepOutput


class UnavailableCode(StrEnum):
    # The wire value is fixed by the HTTP contract ("comfy_down"); the member name stays backend-neutral.
    BACKEND_DOWN = "comfy_down"  # the backend service cannot be reached, or answers with errors
    MISSING_FILE = "missing_file"  # a model / weight file the route needs is not installed
    MISSING_NODE = "missing_node"  # a node, node input or option the route needs is not provided


@dataclass(frozen=True, slots=True)
class Unavailable:
    """Why a route cannot run right now. message is one line for people; raw is the missing path / node or the original error."""

    code: UnavailableCode
    message: str
    raw: str


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """What the backend can do, independent of its current state. The business layer's own limits must fit inside these."""

    routes: frozenset[Route]
    max_images: int  # most images one step can take, canvas / base image included
    size_multiple: int  # output width and height must be multiples of this
    max_seed: int  # seeds are integers in [0, max_seed]


@dataclass(frozen=True, slots=True)
class BackendStatus:
    backend: str  # backend name, e.g. "comfyui"
    host: str  # e.g. "127.0.0.1:8188"
    online: bool
    version: str | None  # the backend's own version string, None when offline
    routes: Mapping[Route, Unavailable | None]  # None = available


class BackendUnavailable(Exception):
    """The backend cannot run the step: it is down, a route lacks a file / node, or the connection dropped mid-run."""

    def __init__(self, problem: Unavailable) -> None:
        super().__init__(f"{problem.code}: {problem.message}")
        self.problem = problem

    @property
    def code(self) -> UnavailableCode:
        return self.problem.code

    @property
    def message(self) -> str:
        return self.problem.message

    @property
    def raw(self) -> str:
        return self.problem.raw


class StepFailed(Exception):
    """The backend took the step and it failed (rejected, errored, interrupted or timed out). raw is the backend's
    original error text, unedited."""

    def __init__(self, raw: str) -> None:
        super().__init__(raw)
        self.raw = raw


class UnexpectedResponse(Exception):
    """The backend answered in a form this implementation does not understand (a bug or an incompatible version).
    raw is what it sent."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(f"{message}: {raw}")
        self.raw = raw


class CleanupFailed(Exception):
    """The step succeeded and output is its finished image, but the backend could not remove its temporary files."""

    def __init__(self, output: StepOutput, reason: str) -> None:
        super().__init__(f"the image was generated, but cleaning up after it failed: {reason}")
        self.output = output
        self.reason = reason


@runtime_checkable
class ImageBackend(Protocol):
    name: str

    def capabilities(self) -> BackendCapabilities: ...

    async def status(self) -> BackendStatus:
        """Live state: reachable or not, version, and per-route availability. Does not generate anything.

        An unreachable or erroring backend is a result (online=False, every route BACKEND_DOWN with raw kept), not an
        exception. Raises UnexpectedResponse only if the backend answers in a form it cannot read.
        """
        ...

    async def run(self, step: StepInput, on_progress: ProgressCallback | None = None) -> StepOutput:
        """Run one step and return its image.

        Raises, before contacting the backend:
          ValueError (imaging.ImageError included) — the step is malformed: images / mask do not fit the route, an
            image cannot be decoded, the mask is not binary or not the base image's size, size or seed out of range.
        Raises while running:
          BackendUnavailable — before starting (down, missing file / node), or mid-run when the connection drops
            (code BACKEND_DOWN).
          StepFailed — the backend rejected or failed the step, or it timed out; raw is the backend's text.
          UnexpectedResponse — the backend answered in a form this implementation cannot read.
          CleanupFailed — the image was made (e.output) but temporary files could not be removed.
        A cleanup problem after any other failure is added as a note to that failure, never raised in its place.
        If the calling task is cancelled, the backend's work for the step is stopped before CancelledError propagates.
        """
        ...
