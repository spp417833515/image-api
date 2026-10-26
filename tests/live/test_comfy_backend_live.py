"""ComfyBackend against the real ComfyUI at ComfyConfig().base_url. No mocks; every test leaves no imageapi_ files behind."""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import pytest
from imagegen.logic import imaging
from imagegen.logic.backend import BackendUnavailable, StepFailed, UnavailableCode
from imagegen.logic.comfy.backend import ComfyBackend
from imagegen.logic.comfy.client import ComfyClient
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.types import Route, Size, StepInput
from PIL import Image, ImageChops, ImageDraw, ImageStat

pytestmark = pytest.mark.live

ASSETS = Path(__file__).resolve().parents[2] / "design/assets"
CONFIG = ComfyConfig()
RED_FOX = (
    "A red fox sitting in fresh snow at golden hour, soft side light, shallow depth of field, 85mm lens. No lettering."
)
# Mask route acceptance, mean |difference| per pixel on 0–255. Outside the white area: the user's threshold
# (ppwiki decision 5; measured up to 4.25 on line art with the base image in the encoder). Inside: it really changed.
MAX_OUTSIDE_DRIFT = 5.0
MIN_INSIDE_CHANGE = 10.0


def imageapi_files() -> set[Path]:
    return {p for d in (CONFIG.input_dir, CONFIG.output_dir) for p in d.glob(f"{CONFIG.file_prefix}*")}


@pytest.fixture(autouse=True)
def leaves_no_files_behind():
    before = imageapi_files()
    yield
    assert imageapi_files() - before == set()


def output_size(image_path: Path) -> Size:
    """How a caller sizes a ref / mask step: long edge ≤ 1920, then the nearest multiple of 32."""
    size = imaging.size_of(imaging.decode(image_path))
    return imaging.round_to_multiple(imaging.scale_down(size, 1920), 32)


async def test_status_online_with_every_route_available() -> None:
    status = await ComfyBackend().status()
    assert status.online and status.backend == "comfyui" and status.host == CONFIG.host
    assert status.version
    assert status.routes == {Route.T2I: None, Route.REF: None, Route.MASK: None}


async def test_text_to_image_red_fox() -> None:
    progress: list[float] = []
    out = await ComfyBackend().run(StepInput(Route.T2I, RED_FOX, (), None, 42, Size(1024, 1024)), progress.append)
    assert (out.width, out.height, out.seed) == (1024, 1024, 42)
    assert imaging.size_of(imaging.decode(out.png, formats={"PNG"})) == Size(1024, 1024)
    assert progress and progress == sorted(progress) and progress[-1] == 1.0 and progress[0] >= 0.0


async def test_reference_edit_outputs_the_rounded_canvas_size() -> None:
    sheet = ASSETS / "sheet.jpg"
    size = output_size(sheet)
    assert size == Size(960, 544)
    instruction = "Change the character's robe to a deep red color, keep everything else the same."
    out = await ComfyBackend().run(StepInput(Route.REF, instruction, (sheet,), None, 7, size))
    assert (out.width, out.height) == (960, 544)
    assert imaging.size_of(imaging.decode(out.png, formats={"PNG"})) == Size(960, 544)


async def run_mask_step(base_path: Path, mask: Image.Image, instruction: str, seed: int):
    """Run the mask route; return (base as sent, result, mask as sent), all at the output size."""
    size = output_size(base_path)
    out = await ComfyBackend().run(StepInput(Route.MASK, instruction, (base_path,), imaging.to_png(mask), seed, size))
    assert (out.width, out.height) == (size.width, size.height)
    base = imaging.resize(imaging.decode(base_path).convert("RGB"), size)
    return base, imaging.decode(out.png).convert("RGB"), imaging.resize_mask(mask, size)


def drift(base: Image.Image, result: Image.Image, mask: Image.Image, label: str) -> tuple[float, float]:
    """Mean |difference| inside and outside the white area of mask."""
    diff = ImageChops.difference(base, result).convert("L")
    inside = ImageStat.Stat(diff, mask=mask).mean[0]
    outside = ImageStat.Stat(diff, mask=mask.point(lambda v: 255 - v)).mean[0]
    print(f"{label}: mean |diff| inside={inside:.2f} outside={outside:.2f} (0-255, limit outside {MAX_OUTSIDE_DRIFT})")
    return inside, outside


async def test_mask_rewrite_changes_only_the_white_area_of_a_photo() -> None:
    mask = imaging.binarize_mask(imaging.decode(ASSETS / "fox-mask.png"), 0.5)
    base, result, sent_mask = await run_mask_step(
        ASSETS / "fox.jpg", mask, "A white arctic fox sitting in fresh snow.", 7
    )
    inside, outside = drift(base, result, sent_mask, "photo fox.jpg")
    assert outside <= MAX_OUTSIDE_DRIFT
    assert inside >= MIN_INSIDE_CHANGE


async def test_mask_rewrite_changes_only_the_white_area_of_line_art() -> None:
    sheet = ASSETS / "sheet.jpg"  # 960x540 → 960x544
    mask = Image.new("L", imaging.decode(sheet).size, 0)
    ImageDraw.Draw(mask).rectangle((75, 85, 255, 430), fill=255)  # the large figure's robe
    base, result, sent_mask = await run_mask_step(sheet, mask, "把长袍改成深红色", 7)
    assert result.size == (960, 544)
    inside, outside = drift(base, result, sent_mask, "line art sheet.jpg")
    assert outside <= MAX_OUTSIDE_DRIFT
    assert inside >= MIN_INSIDE_CHANGE

    def redness(image: Image.Image) -> float:
        red, green, blue = ImageStat.Stat(image, mask=sent_mask).mean
        return red - (green + blue) / 2

    print(f"robe redness: {redness(base):.1f} → {redness(result):.1f}")
    assert redness(result) - redness(base) >= 20  # the instruction took effect: the robe turned red


async def test_comfyui_rejection_is_step_failed_with_its_original_text() -> None:
    backend = ComfyBackend(ComfyConfig(steps=0))
    with pytest.raises(StepFailed) as caught:
        await backend.run(StepInput(Route.T2I, "x", (), None, 1, Size(64, 64)))
    assert "prompt_outputs_failed_validation" in caught.value.raw
    assert "Value 0 smaller than min of 1" in caught.value.raw


async def test_missing_model_file_is_unavailable_with_its_path() -> None:
    backend = ComfyBackend(ComfyConfig(unet="imageapi_not_installed.safetensors"))
    status = await backend.status()
    assert status.online
    for problem in status.routes.values():
        assert problem is not None and problem.code is UnavailableCode.MISSING_FILE
    with pytest.raises(BackendUnavailable) as caught:
        await backend.run(StepInput(Route.T2I, "x", (), None, 1, Size(64, 64)))
    assert caught.value.code is UnavailableCode.MISSING_FILE
    assert caught.value.raw == "models/diffusion_models/imageapi_not_installed.safetensors"


async def test_unreachable_comfyui_is_comfy_down() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    backend = ComfyBackend(ComfyConfig(base_url=f"http://127.0.0.1:{port}"))
    status = await backend.status()
    assert not status.online and status.version is None
    assert all(p is not None and p.code is UnavailableCode.BACKEND_DOWN for p in status.routes.values())
    with pytest.raises(BackendUnavailable) as caught:
        await backend.run(StepInput(Route.T2I, "x", (), None, 1, Size(64, 64)))
    assert caught.value.code is UnavailableCode.BACKEND_DOWN
    assert str(port) in caught.value.raw


async def our_prompts_in_comfyui() -> list[str]:
    """Prompts ComfyUI is running or holding whose outputs would be named with our file prefix."""
    async with ComfyClient(CONFIG) as client:
        queue = await client.queue()
    return [
        item[1]
        for key in ("queue_running", "queue_pending")
        for item in queue[key]
        if any(str(n["inputs"].get("filename_prefix", "")).startswith(CONFIG.file_prefix) for n in item[2].values())
    ]


async def test_cancelled_run_interrupts_comfyui_before_it_returns() -> None:
    sampling = asyncio.Event()
    step = StepInput(Route.T2I, RED_FOX, (), None, 5, Size(1536, 864))  # ~19 s when left to finish
    task = asyncio.create_task(ComfyBackend().run(step, lambda progress: sampling.set()))
    await asyncio.wait_for(sampling.wait(), 300)
    cancelled_at = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    took = time.monotonic() - cancelled_at
    assert await our_prompts_in_comfyui() == []
    print(f"cancelled run returned {took:.1f}s after cancel(), with ComfyUI no longer holding it")
    assert took < 10, f"{took:.1f}s: was the prompt interrupted, or left to finish?"
    await asyncio.sleep(2)  # a prompt left running would write its output about now; the fixture checks
