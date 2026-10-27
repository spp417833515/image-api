"""Which routes can run now, and the /api/status data.

Sources: the backend's live status (one /object_info pull, about 2 MB for ComfyUI), the installed version, and the
upgrade state files. The first two are cached together for MAX_AGE_S; the state files are re-read on every call
(small local files), so an upgrade blocks generation the moment it starts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..logic.backend import BackendStatus, ImageBackend
from ..logic.types import Route
from .models import ImageSize, UnavailableReason
from .routing import ROUTE_NAMES, Limits
from .status_models import LimitsInfo, RoutesState, RouteState, SizePreset, StatusResponse
from .version_state import RECOVER_COMMAND, Interrupted, Upgrading, Version, VersionState

MAX_AGE_S = 5.0

logger = logging.getLogger(__name__)

# Returns the installed version as any object with .tag (str | None) and .commit (str).
type VersionReader = Callable[[], object]


@dataclass(frozen=True, slots=True)
class RouteProblem:
    code: UnavailableReason
    message: str  # one plain sentence for people, naming what is missing
    raw: str  # the missing path / node, or the original error


@dataclass(frozen=True, slots=True)
class Snapshot:
    backend: BackendStatus
    version: Version


class Availability:
    def __init__(
        self,
        backend: ImageBackend,
        read_version: VersionReader,
        state: VersionState,
        limits: Limits,
        max_age_s: float = MAX_AGE_S,
    ) -> None:
        self._backend = backend
        self._read_version = read_version
        self._state = state
        self._limits = limits
        self._max_age_s = max_age_s
        self._snapshot: Snapshot | None = None
        self._fetched_at = 0.0
        self._inflight: asyncio.Task[Snapshot] | None = None

    async def refresh(self) -> Snapshot:
        """Ask the backend now (joining a request already on its way)."""
        return await asyncio.shield(self._start_fetch())

    async def current(self) -> Snapshot:
        """At most MAX_AGE_S old: what /api/status shows."""
        if self._snapshot is None or self._is_stale():
            return await self.refresh()
        return self._snapshot

    async def cached(self) -> Snapshot:
        """For planning, which must not wait on the backend: the last snapshot, refreshed in the background when
        stale. Only the very first call, before anything is known, waits for the backend."""
        if self._snapshot is None:
            return await self.refresh()
        if self._is_stale():
            self._start_fetch()
        return self._snapshot

    def refresh_soon(self) -> None:
        """Something failed against the backend: look again now instead of waiting for the cache to expire."""
        self._fetched_at = float("-inf")
        self._start_fetch()

    def problems(self, snapshot: Snapshot, block: Upgrading | Interrupted | None) -> dict[Route, RouteProblem | None]:
        """Per route the backend supports: None when it can run now. block: VersionState.generation_block()."""
        if block is not None:
            return dict.fromkeys(self._limits.routes, _blocked(block))
        found: dict[Route, RouteProblem | None] = {}
        for route in self._limits.routes:
            if route not in snapshot.backend.routes:
                raise ValueError(
                    f"backend {snapshot.backend.backend!r} declares route {route} but its status has no entry for it: "
                    f"{dict(snapshot.backend.routes)}"
                )
            problem = snapshot.backend.routes[route]
            found[route] = (
                None if problem is None else RouteProblem(UnavailableReason(problem.code), problem.message, problem.raw)
            )
        return found

    async def status(self) -> StatusResponse:
        snapshot = await self.current()
        block = self._state.generation_block()
        problems = self.problems(snapshot, block)
        limits = self._limits

        def state(route: Route) -> RouteState:
            if route not in limits.routes:
                return RouteState(available=False, reason=f"当前生图后端不支持{ROUTE_NAMES[route]}")
            problem = problems[route]
            return RouteState(available=problem is None, reason=None if problem is None else problem.message)

        return StatusResponse(
            backend=snapshot.backend.backend,
            host=snapshot.backend.host,
            online=snapshot.backend.online,
            upgrading=block if isinstance(block, Upgrading) else None,
            version=snapshot.version,
            last_check=self._state.last_check(),
            last_operation=self._state.last_operation(),
            routes=RoutesState(t2i=state(Route.T2I), ref=state(Route.REF), mask=state(Route.MASK)),
            limits=LimitsInfo(
                max_images=limits.max_images,
                max_edge=limits.max_edge,
                size_multiple=limits.size_multiple,
                default_size=ImageSize(width=limits.default_size.width, height=limits.default_size.height),
            ),
            size_presets=[SizePreset(label=label, width=s.width, height=s.height) for label, s in limits.presets],
        )

    def _is_stale(self) -> bool:
        return time.monotonic() - self._fetched_at > self._max_age_s

    def _start_fetch(self) -> asyncio.Task[Snapshot]:
        if self._inflight is None:
            self._inflight = asyncio.create_task(self._fetch())
            self._inflight.add_done_callback(self._fetch_done)
        return self._inflight

    async def _fetch(self) -> Snapshot:
        status = await self._backend.status()
        installed = await asyncio.to_thread(self._read_version)
        snapshot = Snapshot(status, Version.model_validate(installed, from_attributes=True))
        self._snapshot, self._fetched_at = snapshot, time.monotonic()
        return snapshot

    def _fetch_done(self, task: asyncio.Task[Snapshot]) -> None:
        self._inflight = None
        # Retrieved here so a refresh nobody awaited (planning's background one) is still reported.
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("reading the backend status / installed version failed", exc_info=error)


def _blocked(block: Upgrading | Interrupted) -> RouteProblem:
    match block:
        case Upgrading(target=target, since=since):
            return RouteProblem(
                UnavailableReason.UPGRADING,
                f"ComfyUI 正在升级到 {target}，升级完再生成",
                f"upgrading to {target} since {since.isoformat()}",
            )
        case Interrupted(target=target, since=since, pid=pid):
            started = since.astimezone().strftime("%Y-%m-%d %H:%M")
            return RouteProblem(
                UnavailableReason.UPGRADING,
                f"上次升级到 {target} 中断了（{started} 开始），ComfyUI 停在未检查的状态：运行 {RECOVER_COMMAND} 恢复",
                f"the upgrade to {target} since {since.isoformat()} was interrupted (process {pid} is gone); "
                f"run `{RECOVER_COMMAND}`",
            )
