"""POST /api/images (upload) and GET /api/images/{id} (the image bytes)."""

from __future__ import annotations

from fastapi import APIRouter, UploadFile
from fastapi.responses import FileResponse
from imagegen.biz.models import IMAGES_PATH, UploadedImage
from imagegen.biz.service import ImageGenService


def images_router(service: ImageGenService) -> APIRouter:
    router = APIRouter()

    @router.post(IMAGES_PATH, status_code=201)
    async def upload_image(file: UploadFile) -> UploadedImage:
        return await service.upload_image(await file.read())

    @router.get(IMAGES_PATH + "/{image_id}", response_class=FileResponse)
    async def image(image_id: str) -> FileResponse:
        # The page's download button takes the file name (with its extension) from Content-Disposition.
        return FileResponse(
            service.image_path(image_id),
            media_type="image/png",
            filename=f"{image_id}.png",
            content_disposition_type="inline",
        )

    return router
