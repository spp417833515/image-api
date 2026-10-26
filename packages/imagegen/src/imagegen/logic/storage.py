"""Image storage: PNG files plus one metadata JSON each, under a data directory chosen by the caller.

Layout: <root>/<id>.png and <root>/<id>.json. The JSON is written last, so its presence marks a complete record.
Ids are "img_" + 16 hex digits of the creation time in ns + 6 random hex digits, so they sort by creation time.
"""

from __future__ import annotations

import io
import os
import re
import secrets
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self

from PIL import Image
from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from .types import Route

_ID = re.compile(r"img_[0-9a-f]{22}")


class ImageKind(StrEnum):
    UPLOAD = "upload"
    MASK = "mask"
    OUTPUT = "output"


class ImageMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: ImageKind
    width: int
    height: int
    created_at: AwareDatetime
    route: Route | None = None
    seed: int | None = None
    instruction: str | None = None
    job_id: str | None = None
    step: int | None = None

    @model_validator(mode="after")
    def _output_has_provenance(self) -> Self:
        if self.kind is ImageKind.OUTPUT and (self.route is None or self.seed is None):
            raise ValueError(f"an output image needs route and seed, got route={self.route} seed={self.seed}")
        return self


class ImageNotFound(KeyError):
    def __init__(self, image_id: str) -> None:
        super().__init__(image_id)
        self.image_id = image_id

    def __str__(self) -> str:
        return f"no stored image with id {self.image_id!r}"


def is_image_id(value: str) -> bool:
    return _ID.fullmatch(value) is not None


class ImageStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._last_ns = 0

    def save(
        self,
        png: bytes,
        *,
        kind: ImageKind,
        route: Route | None = None,
        seed: int | None = None,
        instruction: str | None = None,
        job_id: str | None = None,
        step: int | None = None,
    ) -> ImageMeta:
        width, height = _png_size(png)
        ns = max(time.time_ns(), self._last_ns + 1)
        self._last_ns = ns
        meta = ImageMeta(
            id=f"img_{ns:016x}{secrets.token_hex(3)}",
            kind=kind,
            width=width,
            height=height,
            created_at=datetime.fromtimestamp(ns / 1e9, UTC),
            route=route,
            seed=seed,
            instruction=instruction,
            job_id=job_id,
            step=step,
        )
        _write_atomic(self._png(meta.id), png)
        _write_atomic(self._json(meta.id), meta.model_dump_json(indent=2).encode())
        return meta

    def path(self, image_id: str) -> Path:
        """Path of the stored PNG; raises ImageNotFound for unknown or malformed ids."""
        self._require(image_id)
        return self._png(image_id)

    def read_bytes(self, image_id: str) -> bytes:
        return self.path(image_id).read_bytes()

    def meta(self, image_id: str) -> ImageMeta:
        self._require(image_id)
        return ImageMeta.model_validate_json(self._json(image_id).read_bytes())

    def history(self, *, limit: int = 50, before: str | None = None) -> list[ImageMeta]:
        """Output images, newest first; with before, only those created before that id."""
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        if before is not None and not is_image_id(before):
            raise ValueError(f"before must be an image id like img_<22 hex>, got {before!r}")
        ids = sorted((p.stem for p in self.root.glob("img_*.json") if is_image_id(p.stem)), reverse=True)
        found: list[ImageMeta] = []
        for image_id in ids:
            if before is not None and image_id >= before:
                continue
            meta = self.meta(image_id)
            if meta.kind is ImageKind.OUTPUT:
                found.append(meta)
                if len(found) == limit:
                    break
        return found

    def _require(self, image_id: str) -> None:
        if not is_image_id(image_id) or not self._json(image_id).is_file():
            raise ImageNotFound(image_id)

    def _png(self, image_id: str) -> Path:
        return self.root / f"{image_id}.png"

    def _json(self, image_id: str) -> Path:
        return self.root / f"{image_id}.json"


def _png_size(png: bytes) -> tuple[int, int]:
    try:
        with Image.open(io.BytesIO(png), formats=["PNG"]) as image:
            return image.size
    except OSError as exc:
        raise ValueError(f"ImageStore stores PNG only, got {len(png)} bytes starting {png[:12]!r}: {exc}") from exc


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
