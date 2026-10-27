"""The HTTP API against the real ComfyUI, no mocks: the server runs in-process (uvicorn, free port) or as the real
`image-api serve`, and every call goes over HTTP exactly as the page makes it. No imageapi_ files are left behind."""

from __future__ import annotations

import io
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from imagegen.biz.routing import MASK_FEATHER_PX, MASK_THRESHOLD
from imagegen.biz.version_checks import mask_drift
from imagegen.factory import create_service
from imagegen.logic import imaging
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.types import Size
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat
from thresholds import MAX_OUTSIDE_DRIFT, MIN_INSIDE_CHANGE  # sibling module in tests/live

from image_api.web.app import create_app

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "design/assets"
CONFIG = ComfyConfig()
RED_FOX = (
    "A red fox sitting in fresh snow at golden hour, soft side light, shallow depth of field, 85mm lens. No lettering."
)
JOB_TIMEOUT_S = 900.0


def imageapi_files() -> set[Path]:
    return {p for d in (CONFIG.input_dir, CONFIG.output_dir) for p in d.glob(f"{CONFIG.file_prefix}*")}


@pytest.fixture(autouse=True)
def leaves_no_files_behind():
    before = imageapi_files()
    yield
    assert imageapi_files() - before == set()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def running_api(data_dir: Path, comfy: ComfyConfig | None = None) -> Iterator[httpx.Client]:
    """The app on a free local port, served by uvicorn in a thread, for the duration of the block."""
    port = free_port()
    config = uvicorn.Config(
        create_app(create_service(data_dir, comfy)), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=f"uvicorn-{port}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=JOB_TIMEOUT_S)


@pytest.fixture
def api(tmp_path) -> Iterator[httpx.Client]:
    with running_api(tmp_path / "data") as client:
        yield client


def upload(client: httpx.Client, data: bytes, filename: str) -> dict:
    response = client.post("/api/images", files={"file": (filename, data)})
    assert response.status_code == 201, response.text
    return response.json()


def plan(client: httpx.Client, request: dict) -> dict:
    response = client.post("/api/plan", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def run_job(client: httpx.Client, request: dict) -> dict:
    accepted = client.post("/api/generate", json=request)
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    deadline = time.monotonic() + JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} still {job['state']} after {JOB_TIMEOUT_S} s: {job}")


def fetch(client: httpx.Client, url: str) -> Image.Image:
    response = client.get(url)
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert ".png" in response.headers["content-disposition"]
    with Image.open(io.BytesIO(response.content)) as image:
        image.load()
        return image.convert("RGB")


def done_image(client: httpx.Client, step: dict) -> Image.Image:
    assert step["state"] == "done" and step["error"] is None, step
    image = fetch(client, step["image"]["url"])
    assert image.size == (step["image"]["width"], step["image"]["height"])
    return image


def solid_png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------- the three routes
def test_red_fox_is_text_to_image(api) -> None:
    request = {"first_step": 1, "steps": [{"instruction": RED_FOX}]}
    planned = plan(api, request)["steps"][0]
    assert (planned["route"], planned["output"]) == ("t2i", {"width": 1024, "height": 1024})
    job = run_job(api, request)
    assert job["state"] == "done", job
    step = job["steps"][0]
    assert step["route"] == "t2i" and isinstance(step["image"]["seed"], int)
    assert done_image(api, step).size == (1024, 1024)
    history = api.get("/api/history").json()["items"]
    assert history[0]["id"] == step["image"]["id"] and history[0]["instruction"] == RED_FOX


def test_one_reference_image_is_ref_and_follows_its_size(api) -> None:
    sheet = upload(api, (ASSETS / "sheet.jpg").read_bytes(), "sheet.jpg")
    assert (sheet["width"], sheet["height"]) == (960, 540)
    request = {"steps": [{"instruction": "把长袍改成深红色，其余保持不变", "images": [sheet["id"]], "seed": 7}]}
    planned = plan(api, request)["steps"][0]
    assert (planned["route"], planned["output"]) == ("ref", {"width": 960, "height": 544})
    job = run_job(api, request)
    assert job["state"] == "done", job
    assert job["steps"][0]["image"]["seed"] == 7
    assert done_image(api, job["steps"][0]).size == (960, 544)


def drawn_mask(size: tuple[int, int], shape: str, box: tuple[int, int, int, int]) -> bytes:
    mask = Image.new("L", size, 0)
    getattr(ImageDraw.Draw(mask), shape)(box, fill=255)
    out = io.BytesIO()
    mask.save(out, format="PNG")
    return out.getvalue()


def solid_colour() -> tuple[bytes, bytes]:
    """A solid-colour picture (where the backend's colour shift outside the mask is largest) and a mask on it."""
    out = io.BytesIO()
    Image.new("RGB", (768, 512), (70, 130, 180)).save(out, format="PNG")
    return out.getvalue(), drawn_mask((768, 512), "ellipse", (256, 128, 511, 383))


MASK_CASES = {
    "photo": (
        lambda: ((ASSETS / "fox.jpg").read_bytes(), (ASSETS / "fox-mask.png").read_bytes()),
        "A white arctic fox sitting in fresh snow.",
    ),
    "line-art": (
        lambda: ((ASSETS / "sheet.jpg").read_bytes(), drawn_mask((960, 540), "rectangle", (75, 85, 255, 430))),
        "把长袍改成深红色",
    ),
    "solid": (solid_colour, "A red apple."),
}


@pytest.mark.parametrize("case", list(MASK_CASES))
def test_mask_route_repaints_only_the_white_area(api, tmp_path, case) -> None:
    make_inputs, instruction = MASK_CASES[case]
    base_bytes, mask_bytes = make_inputs()
    base = upload(api, base_bytes, f"{case}-base")
    mask = upload(api, mask_bytes, f"{case}-mask")
    request = {"steps": [{"instruction": instruction, "images": [base["id"]], "mask": mask["id"], "seed": 7}]}
    assert plan(api, request)["steps"][0]["route"] == "mask"
    job = run_job(api, request)
    assert job["state"] == "done", job
    result = done_image(api, job["steps"][0])
    size = Size(*result.size)
    # The original and the mask at the output size, the way the service brings them there.
    stored_base, stored_mask = (imaging.decode(api.get(image["url"]).content) for image in (base, mask))
    original = imaging.resize(imaging.flatten(stored_base, imaging.FLATTEN_BACKGROUND), size)
    white = imaging.resize_mask(imaging.binarize_mask(stored_mask, MASK_THRESHOLD), size)
    reach = white.filter(ImageFilter.MaxFilter(2 * MASK_FEATHER_PX + 1))
    beyond = ImageOps.invert(reach)
    changed_beyond = ImageStat.Stat(ImageChops.difference(original, result), mask=beyond)
    largest = max(high for _, high in changed_beyond.extrema)
    inside, outside = mask_drift(original, result, white)
    save_comparison(tmp_path / "pasteback", case, original, result, white)
    print(
        f"{case}: beyond the {MASK_FEATHER_PX} px band max |diff|={largest} over {changed_beyond.count[0]} px; "
        f"mean inside={inside:.2f} outside={outside:.2f}; images in {tmp_path / 'pasteback'}"
    )
    assert changed_beyond.count[0] > 0 and largest == 0  # outside the white area and the band: the original exactly
    assert inside >= MIN_INSIDE_CHANGE  # the white area really changed
    assert outside <= MAX_OUTSIDE_DRIFT


def save_comparison(directory: Path, case: str, original: Image.Image, result: Image.Image, white: Image.Image) -> None:
    """For a person to look at: original | result | difference ×8, and both edges of the mask zoomed in ×4."""
    directory.mkdir(parents=True, exist_ok=True)
    diff = ImageChops.difference(original, result).point(lambda v: min(255, v * 8))
    width, height = result.size
    sheet = Image.new("RGB", (width * 3, height))
    for column, image in enumerate((original, result, diff)):
        sheet.paste(image, (column * width, 0))
    sheet.save(directory / f"{case}-original-result-diff.png")
    left, top, right, bottom = white.getbbox()
    middle = (top + bottom) // 2
    crops = []
    for x in (left, right):  # the mask's left and right edges, 64 px either side
        box = (max(0, x - 64), max(0, middle - 64), min(width, x + 64), min(height, middle + 64))
        pair = Image.new("RGB", ((box[2] - box[0]) * 2, box[3] - box[1]))
        pair.paste(original.crop(box), (0, 0))
        pair.paste(result.crop(box), (box[2] - box[0], 0))
        crops.append(pair.resize((pair.width * 4, pair.height * 4), Image.Resampling.NEAREST))
    edges = Image.new("RGB", (max(c.width for c in crops), sum(c.height for c in crops)))
    for row, crop in enumerate(crops):
        edges.paste(crop, (0, sum(c.height for c in crops[:row])))
    edges.save(directory / f"{case}-edges-x4.png")


# ---------------------------------------------------------------- several steps
def test_two_steps_in_one_request_hand_over_with_prev(api) -> None:
    request = {
        "first_step": 1,
        "steps": [
            {"instruction": RED_FOX, "width": 512, "height": 512, "seed": 11},
            {"instruction": "Make it a night scene under moonlight, keep the fox.", "images": ["$prev"], "seed": 12},
        ],
    }
    assert [s["route"] for s in plan(api, request)["steps"]] == ["t2i", "ref"]
    job = run_job(api, request)
    assert job["state"] == "done", job
    assert [(s["step"], s["route"]) for s in job["steps"]] == [(1, "t2i"), (2, "ref")]
    first, second = (done_image(api, s) for s in job["steps"])
    assert first.size == second.size == (512, 512)  # step 2 took step 1's image as its canvas


def test_run_step_one_then_paint_a_mask_and_run_step_two_alone(api) -> None:
    first = run_job(
        api, {"first_step": 1, "steps": [{"instruction": RED_FOX, "width": 512, "height": 512, "seed": 21}]}
    )
    assert first["state"] == "done", first
    image = first["steps"][0]["image"]
    mask = Image.new("L", (image["width"], image["height"]), 0)
    ImageDraw.Draw(mask).ellipse((128, 128, 384, 384), fill=255)
    out = io.BytesIO()
    mask.save(out, format="PNG")
    painted = upload(api, out.getvalue(), "mask.png")
    request = {
        "first_step": 2,
        "steps": [{"instruction": "A small snowman.", "images": [image["id"]], "mask": painted["id"]}],
    }
    planned = plan(api, request)
    assert planned["ok"] and planned["steps"][0]["step"] == 2 and planned["steps"][0]["route"] == "mask"
    second = run_job(api, request)
    assert second["state"] == "done", second
    assert [(s["step"], s["route"]) for s in second["steps"]] == [(2, "mask")]
    assert done_image(api, second["steps"][0]).size == (512, 512)


# ---------------------------------------------------------------- failures
def test_comfyui_not_listening_is_503_comfy_down(tmp_path) -> None:
    with running_api(tmp_path / "data", ComfyConfig(base_url=f"http://127.0.0.1:{free_port()}")) as client:
        response = client.post("/api/generate", json={"steps": [{"instruction": RED_FOX}]})
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["code"] == "comfy_down" and body["message"] and body["raw"]
        status = client.get("/api/status").json()
        assert not status["online"] and not status["routes"]["t2i"]["available"]
        assert plan(client, {"steps": [{"instruction": RED_FOX}]})["steps"][0]["unavailable"]["code"] == "comfy_down"


def test_a_missing_model_file_is_503_naming_the_file(tmp_path) -> None:
    """A missing model file blocks the routes that load it and says which file. Since ppwiki decision id=5 (fourth
    revision) the mask route loads no file of its own, so a missing VAE blocks all three routes."""
    missing = "imageapi_not_installed_vae.safetensors"
    path = f"models/vae/{missing}"
    with running_api(tmp_path / "data", ComfyConfig(vae=missing)) as client:
        status = client.get("/api/status").json()
        assert status["online"]
        for route in ("t2i", "ref", "mask"):
            assert not status["routes"][route]["available"] and path in status["routes"][route]["reason"], route
        base = upload(client, solid_png(256, 256, (90, 120, 160)), "base.png")
        mask = Image.new("L", (256, 256), 0)
        ImageDraw.Draw(mask).rectangle((64, 64, 191, 191), fill=255)
        out = io.BytesIO()
        mask.save(out, format="PNG")
        painted = upload(client, out.getvalue(), "mask.png")
        mask_step = {"instruction": "a red ball", "images": [base["id"]], "mask": painted["id"]}
        for step in (mask_step, {"instruction": RED_FOX}):
            unavailable = plan(client, {"steps": [step]})["steps"][0]["unavailable"]
            assert unavailable["code"] == "missing_file" and path in unavailable["message"]
            response = client.post("/api/generate", json={"steps": [step]})
            assert response.status_code == 503, response.text
            body = response.json()
            assert body["code"] == "missing_file" and path in body["message"] and body["raw"] == path


def test_a_step_comfyui_rejects_fails_with_its_number_and_original_text(tmp_path) -> None:
    with running_api(tmp_path / "data", ComfyConfig(steps=0)) as client:
        request = {
            "first_step": 3,
            "steps": [
                {"instruction": RED_FOX, "width": 256, "height": 256},
                {"instruction": "make it night", "images": ["$prev"]},
            ],
        }
        job = run_job(client, request)
        assert job["state"] == "failed"
        failed, skipped = job["steps"]
        assert (failed["step"], failed["state"], skipped["step"], skipped["state"]) == (3, "failed", 4, "skipped")
        assert "prompt_outputs_failed_validation" in failed["error"]["raw"]
        assert "Value 0 smaller than min of 1" in failed["error"]["raw"]
        assert skipped["error"] is None and skipped["image"] is None


# ---------------------------------------------------------------- the real command
def test_image_api_serve_serves_the_page_and_its_files(tmp_path) -> None:
    port = free_port()
    command = [str(Path(sys.executable).parent / "image-api"), "serve", "--data-dir", str(tmp_path / "data")]
    env = {**os.environ, "IMAGE_API_PORT": str(port)}
    log = tmp_path / "serve.log"
    with log.open("wb") as out:
        server = subprocess.Popen(command, env=env, stdout=out, stderr=subprocess.STDOUT, cwd=tmp_path)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
                page = None
                deadline = time.monotonic() + 30
                while page is None:
                    assert server.poll() is None, f"image-api serve exited: {log.read_text()}"
                    assert time.monotonic() < deadline, f"image-api serve did not answer: {log.read_text()}"
                    try:
                        page = client.get("/")
                    except httpx.TransportError:
                        time.sleep(0.2)
                assert page.status_code == 200 and '<script type="module" src="/static/js/main.js">' in page.text
                for asset in ("/static/js/main.js", "/static/js/api.js", "/static/css/theme.css"):
                    assert client.get(asset).status_code == 200, asset
                status = client.get("/api/status").json()
                assert status["online"] and status["version"]["commit"]
        finally:
            server.terminate()
            code = server.wait(timeout=60)
    # uvicorn shuts down cleanly on SIGTERM, then re-raises the signal: exit status -SIGTERM.
    assert code in (0, -signal.SIGTERM) and "Application shutdown complete" in log.read_text(), log.read_text()
