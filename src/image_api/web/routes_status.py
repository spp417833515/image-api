"""GET /api/status and GET /api/history."""

from __future__ import annotations

from fastapi import APIRouter
from imagegen.biz.models import HistoryPage
from imagegen.biz.service import ImageGenService
from imagegen.biz.status_models import StatusResponse


def status_router(service: ImageGenService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/status")
    async def status() -> StatusResponse:
        return await service.status()

    @router.get("/history")
    async def history(limit: int = 50, before: str | None = None) -> HistoryPage:
        return await service.history(limit, before)

    return router
