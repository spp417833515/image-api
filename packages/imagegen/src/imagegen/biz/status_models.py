"""GET /api/status as pydantic models (the rest of the HTTP contract is in models.py)."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from .models import ImageSize
from .version_state import LastCheck, Operation, Upgrading, Version

_MODEL = ConfigDict(frozen=True, extra="forbid")


class RouteState(BaseModel):
    model_config = _MODEL

    available: bool
    reason: str | None  # why not, one plain sentence naming what is missing

    @model_validator(mode="after")
    def _reason_when_unavailable(self) -> Self:
        if self.available == bool(self.reason):
            raise ValueError(f"available={self.available} needs reason only when unavailable, got {self.reason!r}")
        return self


class RoutesState(BaseModel):
    model_config = _MODEL

    t2i: RouteState
    ref: RouteState
    mask: RouteState


class LimitsInfo(BaseModel):
    model_config = _MODEL

    max_images: int
    max_edge: int
    size_multiple: int
    default_size: ImageSize


class SizePreset(BaseModel):
    model_config = _MODEL

    label: str
    width: int
    height: int


class StatusResponse(BaseModel):
    model_config = _MODEL

    backend: str
    host: str
    online: bool
    upgrading: Upgrading | None
    version: Version
    last_check: LastCheck | None
    last_operation: Operation | None
    routes: RoutesState
    limits: LimitsInfo
    size_presets: list[SizePreset]
