"""ImageStore: write / read, metadata, ids sortable by time, history paging over outputs only."""

from __future__ import annotations

import io
import json

import pytest
from imagegen.logic.storage import ImageKind, ImageNotFound, ImageStore, is_image_id
from imagegen.logic.types import Route
from PIL import Image
from pydantic import ValidationError


def png(width: int = 8, height: int = 6) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


def save_output(store: ImageStore, step: int) -> str:
    return store.save(png(), kind=ImageKind.OUTPUT, route=Route.T2I, seed=step, job_id="job_1", step=step).id


def test_save_then_read_back(tmp_path) -> None:
    store = ImageStore(tmp_path / "data")
    data = png(20, 10)
    meta = store.save(
        data, kind=ImageKind.OUTPUT, route=Route.REF, seed=42, instruction="画一只狐狸", job_id="job_x", step=2
    )
    assert is_image_id(meta.id)
    assert (meta.width, meta.height) == (20, 10)
    assert store.read_bytes(meta.id) == data
    assert store.path(meta.id).read_bytes() == data
    assert store.meta(meta.id) == meta


def test_metadata_json_is_complete_and_timezone_aware(tmp_path) -> None:
    store = ImageStore(tmp_path)
    meta = store.save(png(), kind=ImageKind.UPLOAD)
    record = json.loads((tmp_path / f"{meta.id}.json").read_text(encoding="utf-8"))
    assert set(record) == {
        "id",
        "kind",
        "width",
        "height",
        "created_at",
        "route",
        "seed",
        "instruction",
        "job_id",
        "step",
    }
    assert record["kind"] == "upload"
    assert record["created_at"].endswith("Z") or "+" in record["created_at"]


def test_ids_sort_by_creation_time(tmp_path) -> None:
    store = ImageStore(tmp_path)
    ids = [store.save(png(), kind=ImageKind.UPLOAD).id for _ in range(5)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 5


def test_history_is_outputs_only_newest_first(tmp_path) -> None:
    store = ImageStore(tmp_path)
    first = save_output(store, 1)
    store.save(png(), kind=ImageKind.UPLOAD)
    store.save(png(), kind=ImageKind.MASK)
    second = save_output(store, 2)
    assert [m.id for m in store.history()] == [second, first]


def test_history_pages_with_limit_and_before(tmp_path) -> None:
    store = ImageStore(tmp_path)
    ids = [save_output(store, n) for n in range(5)]
    newest_first = ids[::-1]
    page1 = store.history(limit=2)
    page2 = store.history(limit=2, before=page1[-1].id)
    page3 = store.history(limit=2, before=page2[-1].id)
    assert [m.id for m in page1 + page2 + page3] == newest_first
    assert store.history(limit=2, before=ids[0]) == []


def test_history_rejects_bad_paging(tmp_path) -> None:
    store = ImageStore(tmp_path)
    with pytest.raises(ValueError):
        store.history(limit=0)
    with pytest.raises(ValueError):
        store.history(before="../etc")


@pytest.mark.parametrize("image_id", ["img_00000000000000000000aa", "../../etc/passwd", "img_xyz", ""])
def test_unknown_or_malformed_ids_are_not_found(tmp_path, image_id: str) -> None:
    store = ImageStore(tmp_path)
    with pytest.raises(ImageNotFound):
        store.read_bytes(image_id)
    with pytest.raises(ImageNotFound):
        store.meta(image_id)


def test_only_png_is_stored(tmp_path) -> None:
    out = io.BytesIO()
    Image.new("RGB", (4, 4)).save(out, format="JPEG")
    with pytest.raises(ValueError, match="PNG only"):
        ImageStore(tmp_path).save(out.getvalue(), kind=ImageKind.UPLOAD)


def test_output_needs_route_and_seed(tmp_path) -> None:
    with pytest.raises(ValidationError):
        ImageStore(tmp_path).save(png(), kind=ImageKind.OUTPUT)
