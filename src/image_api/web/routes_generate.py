"""POST /api/plan, POST /api/generate, GET /api/jobs/{id}."""

from __future__ import annotations

from fastapi import APIRouter
from imagegen.biz.models import GenerateAccepted, GenerateRequest, Job, PlanResponse
from imagegen.biz.service import ImageGenService


def generate_router(service: ImageGenService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/plan")
    async def plan(request: GenerateRequest) -> PlanResponse:
        return await service.plan(request)

    @router.post("/generate", status_code=202)
    async def generate(request: GenerateRequest) -> GenerateAccepted:
        return await service.generate(request)

    @router.get("/jobs/{job_id}")
    async def job(job_id: str) -> Job:
        return service.job(job_id)

    return router
