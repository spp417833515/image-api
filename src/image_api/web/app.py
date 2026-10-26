"""The FastAPI app: the HTTP contract over an ImageGenService, plus the page at / and its files at /static."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from imagegen.biz.service import ImageGenService

from .errors import install_error_handlers
from .routes_generate import generate_router
from .routes_images import images_router
from .routes_status import status_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(service: ImageGenService) -> FastAPI:
    """The service's job worker runs for the app's lifetime (started and stopped by the server's lifespan)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with service:
            yield

    app = FastAPI(title="image-api", summary="生图入口：自动路由 · 多步 · 可插拔后端", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(images_router(service))
    app.include_router(generate_router(service))
    app.include_router(status_router(service))

    @app.get("/", include_in_schema=False)
    async def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
