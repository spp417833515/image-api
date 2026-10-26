"""ComfyBackend's error semantics without a network: ComfyClient's calls and run_prompt are answered locally, and
ComfyUI's input / output folders live under tmp_path."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from imagegen.logic.backend import (
    BackendUnavailable,
    CleanupFailed,
    StepFailed,
    UnavailableCode,
    UnexpectedResponse,
)
from imagegen.logic.comfy import backend as comfy_backend
from imagegen.logic.comfy import graphs
from imagegen.logic.comfy.backend import ComfyBackend
from imagegen.logic.comfy.client import ComfyClient, ComfyHTTPError, ComfyUnreachable
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.types import Route, Size, StepInput
from offline import UNREACHABLE_URL
from PIL import Image, ImageFile, ImageOps

SAMPLE = json.loads((Path(__file__).parent / "fixtures/object_info_0.42.0.json").read_text(encoding="utf-8"))
COMFY_TEXT = "KSampler (node sample): torch.OutOfMemoryError: CUDA out of memory"


def png(width: int, height: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(out, format="PNG")
    return out.getvalue()


class LocalComfy:
    """Answers ComfyClient calls; writes uploads / outputs into the folders unless told the files go elsewhere."""

    def __init__(self, config: ComfyConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config = config
        self.write_uploads = True
        self.write_outputs = True
        self.failure: BaseException | None = None
        self.history: dict[str, Any] | None = None
        self.output_png = png(64, 64)  # what view() returns
        saved_png = png(64, 64)  # made here, outside the event loop
        config.input_dir.mkdir(parents=True)
        config.output_dir.mkdir(parents=True)
        local = self

        async def object_info(client: ComfyClient) -> dict[str, Any]:
            return SAMPLE

        async def upload_image(client: ComfyClient, name: str, data: bytes) -> str:
            if local.write_uploads:
                (config.input_dir / name).write_bytes(data)
            return name

        async def run_prompt(client: ComfyClient, graph: graphs.Graph, cfg: ComfyConfig, **kwargs: Any) -> str:
            if local.failure is not None:
                raise local.failure
            prefix = graph[graphs.SAVE]["inputs"]["filename_prefix"]
            local.output = f"{prefix}_00001_.png"
            if local.write_outputs:
                (config.output_dir / local.output).write_bytes(saved_png)
            return "pid"

        async def history(client: ComfyClient, prompt_id: str) -> dict[str, Any]:
            if local.history is not None:
                return local.history
            image = {"filename": local.output, "subfolder": "", "type": "output"}
            return {prompt_id: {"status": {"status_str": "success"}, "outputs": {graphs.SAVE: {"images": [image]}}}}

        async def view(client: ComfyClient, filename: str, subfolder: str, type: str) -> bytes:
            return local.output_png

        for name, fake in [
            ("object_info", object_info),
            ("upload_image", upload_image),
            ("history", history),
            ("view", view),
        ]:
            monkeypatch.setattr(ComfyClient, name, fake)
        monkeypatch.setattr(comfy_backend, "run_prompt", run_prompt)

    def leftovers(self) -> list[Path]:
        return sorted([*self.config.input_dir.iterdir(), *self.config.output_dir.iterdir()])


@pytest.fixture
def comfy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalComfy:
    return LocalComfy(ComfyConfig(base_url=UNREACHABLE_URL, root=tmp_path / "ComfyUI"), monkeypatch)


def ref_step() -> StepInput:
    return StepInput(Route.REF, "x", (png(64, 64),), None, 1, Size(64, 64))


async def test_success_cleans_up_and_returns_the_image(comfy: LocalComfy) -> None:
    output = await ComfyBackend(comfy.config).run(ref_step())
    assert (output.width, output.height, output.seed) == (64, 64, 1)
    assert comfy.leftovers() == []


async def test_failure_cleans_up_and_keeps_comfyui_text(comfy: LocalComfy) -> None:
    comfy.failure = StepFailed(COMFY_TEXT)
    with pytest.raises(StepFailed) as caught:
        await ComfyBackend(comfy.config).run(ref_step())
    assert caught.value.raw == COMFY_TEXT and not getattr(caught.value, "__notes__", [])
    assert comfy.leftovers() == []


async def test_cleanup_failure_after_a_failure_is_a_note_on_it(comfy: LocalComfy) -> None:
    comfy.write_uploads = comfy.write_outputs = False  # the files are not where config.root says
    comfy.failure = StepFailed(COMFY_TEXT)
    with pytest.raises(StepFailed) as caught:
        await ComfyBackend(comfy.config).run(ref_step())
    assert caught.value.raw == COMFY_TEXT
    assert any("cleaning up this run's ComfyUI files also failed" in note for note in caught.value.__notes__)


async def test_cleanup_failure_after_success_keeps_the_image(comfy: LocalComfy) -> None:
    comfy.write_uploads = comfy.write_outputs = False
    with pytest.raises(CleanupFailed, match="the image was generated") as caught:
        await ComfyBackend(comfy.config).run(ref_step())
    output = caught.value.output
    assert (output.width, output.height) == (64, 64) and output.png.startswith(b"\x89PNG")
    assert "is ComfyConfig.root" in caught.value.reason


async def test_unexpected_history_is_a_named_error(comfy: LocalComfy) -> None:
    comfy.history = {}
    with pytest.raises(UnexpectedResponse, match="no history"):
        await ComfyBackend(comfy.config).run(ref_step())
    assert comfy.leftovers() == []


async def test_http_500_before_the_step_is_backend_down_with_raw(comfy: LocalComfy, monkeypatch) -> None:
    async def erroring(client: ComfyClient) -> dict[str, Any]:
        raise ComfyHTTPError("GET", "http://127.0.0.1:8188/object_info", 500, "Server got itself in trouble")

    monkeypatch.setattr(ComfyClient, "object_info", erroring)
    with pytest.raises(BackendUnavailable) as caught:
        await ComfyBackend(comfy.config).run(ref_step())
    assert caught.value.code is UnavailableCode.BACKEND_DOWN
    assert "HTTP 500" in caught.value.message and "Server got itself in trouble" in caught.value.raw


async def test_status_with_http_500_is_offline_with_raw(monkeypatch) -> None:
    async def erroring(client: ComfyClient) -> dict[str, Any]:
        raise ComfyHTTPError("GET", "http://127.0.0.1:8188/system_stats", 500, "Server got itself in trouble")

    monkeypatch.setattr(ComfyClient, "system_stats", erroring)
    status = await ComfyBackend(ComfyConfig(base_url=UNREACHABLE_URL)).status()
    assert not status.online and status.version is None
    for problem in status.routes.values():
        assert problem is not None and problem.code is UnavailableCode.BACKEND_DOWN
        assert "HTTP 500" in problem.message and "Server got itself in trouble" in problem.raw


def test_backend_down_keeps_the_contract_wire_value() -> None:
    assert UnavailableCode.BACKEND_DOWN == "comfy_down"


async def test_http_error_during_the_step_is_step_failed_with_comfyui_body(comfy: LocalComfy, monkeypatch) -> None:
    async def missing(client: ComfyClient, filename: str, subfolder: str, type: str) -> bytes:
        raise ComfyHTTPError("GET", "http://127.0.0.1:8188/view", 404, "404: Not Found (ComfyUI body)")

    monkeypatch.setattr(ComfyClient, "view", missing)
    with pytest.raises(StepFailed) as caught:
        await ComfyBackend(comfy.config).run(ref_step())
    assert "HTTP 404" in caught.value.raw and "404: Not Found (ComfyUI body)" in caught.value.raw
    assert comfy.leftovers() == []


async def test_unavailable_route_is_refused_before_uploading(comfy: LocalComfy, monkeypatch) -> None:
    info = json.loads(json.dumps(SAMPLE))
    del info["ZImageFunControlnet"]

    async def object_info(client: ComfyClient) -> dict[str, Any]:
        return info

    monkeypatch.setattr(ComfyClient, "object_info", object_info)
    mask = io.BytesIO()
    Image.new("L", (64, 64), 255).save(mask, format="PNG")
    step = StepInput(Route.MASK, "x", (png(64, 64),), mask.getvalue(), 1, Size(64, 64))
    with pytest.raises(BackendUnavailable) as caught:
        await ComfyBackend(comfy.config).run(step)
    assert caught.value.code is UnavailableCode.MISSING_NODE and caught.value.raw == "ZImageFunControlnet"
    assert comfy.leftovers() == []


async def test_output_file_counts_toward_cleanup(comfy: LocalComfy) -> None:
    comfy.write_outputs = False  # ComfyUI reported an output, but it is not in <root>/output
    with pytest.raises(CleanupFailed) as caught:
        await ComfyBackend(comfy.config).run(ref_step())
    assert "expected 1 file(s) in" in caught.value.reason and "and 1 in" in caught.value.reason
    assert "found 1 and 0" in caught.value.reason


async def test_connection_lost_mid_run_is_backend_down_and_keeps_notes(comfy: LocalComfy) -> None:
    lost = ComfyUnreachable("ws://127.0.0.1:8188/ws", "connection reset mid-run")
    lost.add_note("stopping ComfyUI prompt p also failed, it may still write files: refused")
    comfy.failure = lost
    with pytest.raises(BackendUnavailable) as caught:
        await ComfyBackend(comfy.config).run(ref_step())
    assert caught.value.code is UnavailableCode.BACKEND_DOWN
    assert "connection reset mid-run" in caught.value.raw
    assert "stopping ComfyUI prompt p also failed, it may still write files: refused" in caught.value.__notes__
    assert comfy.leftovers() == []


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        (
            {
                "pid": {
                    "status": {"status_str": "success"},
                    "outputs": {"save": {"images": [{"subfolder": "", "type": "output"}]}},
                }
            },
            "lacks filename",
        ),
        ({"pid": {"status": {"status_str": "success"}, "outputs": {"save": {"images": ["x.png"]}}}}, "unknown shape"),
        ({"pid": ["not", "an", "entry"]}, "unknown shape"),
        ({"pid": {"status": "success", "outputs": {}}}, "unknown shape"),
    ],
)
async def test_odd_history_is_unexpected_response_with_raw(comfy: LocalComfy, history, expected: str) -> None:
    comfy.history = history
    with pytest.raises(UnexpectedResponse, match=expected) as caught:
        await ComfyBackend(comfy.config).run(ref_step())
    assert caught.value.raw
    assert comfy.leftovers() == []


# Pillow entry points that decode, resample, convert or encode pixels: the CPU-heavy part of handling an image.
PIXEL_WORK = [
    (Image, "open"),
    (Image, "alpha_composite"),
    (ImageFile.ImageFile, "load"),
    (Image.Image, "resize"),
    (Image.Image, "convert"),
    (Image.Image, "point"),
    (Image.Image, "copy"),
    (Image.Image, "transpose"),
    (Image.Image, "save"),
    (ImageOps, "exif_transpose"),
]


def on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def pixel_work(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Every Pillow pixel operation from now on, filed under whether it ran on the event loop or in a worker thread."""
    seen: dict[str, list[str]] = {"loop": [], "thread": []}

    def spying(real: Callable[..., Any], label: str) -> Callable[..., Any]:
        def spy(*args: Any, **kwargs: Any) -> Any:
            seen["loop" if on_event_loop() else "thread"].append(label)
            return real(*args, **kwargs)

        return spy

    for owner, name in PIXEL_WORK:
        monkeypatch.setattr(owner, name, spying(getattr(owner, name), f"{getattr(owner, '__name__', owner)}.{name}"))
    return seen


def mask_png(size: int) -> bytes:
    out = io.BytesIO()
    Image.new("L", (size, size), 255).save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize(
    "make_step",
    [
        lambda: StepInput(Route.T2I, "x", (), None, 1, Size(64, 64)),
        lambda: StepInput(Route.REF, "x", (png(64, 64), png(96, 64)), None, 1, Size(64, 64)),
        lambda: StepInput(Route.MASK, "x", (png(64, 64),), mask_png(64), 1, Size(64, 64)),
    ],
    ids=["t2i", "ref", "mask"],
)
async def test_no_image_work_runs_on_the_event_loop(comfy: LocalComfy, pixel_work, make_step) -> None:
    step = make_step()
    pixel_work["loop"].clear()  # building the step's test images does not count
    await ComfyBackend(comfy.config).run(step)
    assert pixel_work["loop"] == [], f"pixel work on the event loop: {pixel_work['loop']}"
    assert pixel_work["thread"], "no pixel work was seen at all; the spies are not in the path"
