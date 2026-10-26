"""What ComfyBackend uploads: images sized so ComfyUI resizes nothing, and run-file cleanup that touches only one run."""

from __future__ import annotations

import io

import pytest
from imagegen.logic import imaging
from imagegen.logic.comfy.backend import ComfyBackend
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.comfy.files import CleanupError, remove_run_files
from imagegen.logic.comfy.prepare import prepare
from imagegen.logic.types import Route, Size, StepInput
from offline import UNREACHABLE_URL
from PIL import Image


def encoded(image: Image.Image, fmt: str = "PNG") -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


def size_of_png(data: bytes) -> Size:
    return imaging.size_of(imaging.decode(data, formats={"PNG"}))


CANVAS = Size(960, 544)


def step(route: Route, images=(), mask=None, size: Size = CANVAS) -> StepInput:
    return StepInput(route, "x", tuple(images), mask, 1, size)


def test_t2i_uploads_nothing() -> None:
    uploads = prepare(step(Route.T2I, size=Size(1024, 1024)), 32)
    assert uploads.images == () and uploads.mask is None


def test_canvas_is_resized_to_the_output_size() -> None:
    canvas = encoded(Image.new("RGB", (960, 540), "red"), "JPEG")
    uploads = prepare(step(Route.REF, [canvas]), 32)
    assert size_of_png(uploads.images[0]) == Size(960, 544)


def test_references_shrink_to_the_canvas_area_at_multiples_of_32() -> None:
    canvas = encoded(Image.new("RGB", (960, 540)))
    big = encoded(Image.new("RGB", (4000, 3000)))
    small = encoded(Image.new("RGB", (300, 200)))
    uploads = prepare(step(Route.REF, [canvas, big, small]), 32)
    big_size, small_size = size_of_png(uploads.images[1]), size_of_png(uploads.images[2])
    assert big_size.area <= 960 * 544 * 1.1
    assert big_size.width % 32 == 0 and big_size.height % 32 == 0
    assert small_size == Size(288, 192)  # not enlarged, only rounded


def test_transparency_is_flattened_over_white() -> None:
    canvas = encoded(Image.new("RGBA", (64, 64), (0, 0, 0, 0)))
    uploads = prepare(step(Route.REF, [canvas], size=Size(64, 64)), 32)
    assert imaging.decode(uploads.images[0]).getpixel((0, 0)) == (255, 255, 255)


def striped_mask(width: int, height: int) -> Image.Image:
    """Alternating 1-pixel black / white rows: a smoothing resize of the height (540 → 544) creates grey levels."""
    mask = Image.new("L", (width, height), 0)
    for y in range(0, height, 2):
        mask.paste(255, (0, y, width, y + 1))
    return mask


def levels(image: Image.Image) -> set[int]:
    return {level for level, count in enumerate(image.convert("L").histogram()) if count}


def test_mask_is_resized_nearest_neighbour_with_its_base_and_stays_binary() -> None:
    base = encoded(Image.new("RGB", (960, 540)))
    mask = encoded(striped_mask(960, 540))
    uploads = prepare(step(Route.MASK, [base], mask), 32)
    assert uploads.mask is not None
    decoded = imaging.decode(uploads.mask)
    assert decoded.mode == "L" and imaging.size_of(decoded) == Size(960, 544)
    assert levels(decoded) == {0, 255}


def test_one_bit_mask_is_accepted() -> None:
    base = encoded(Image.new("RGB", (64, 64)))
    uploads = prepare(step(Route.MASK, [base], encoded(striped_mask(64, 64).convert("1")), Size(64, 64)), 32)
    assert uploads.mask is not None and levels(imaging.decode(uploads.mask)) == {0, 255}


def test_grey_mask_is_rejected_not_guessed() -> None:
    base = encoded(Image.new("RGB", (64, 64)))
    grey = Image.new("L", (64, 64), 0)
    grey.paste(128, (0, 0, 32, 64))
    grey.paste(255, (32, 0, 64, 64))
    with pytest.raises(ValueError, match=r"only 0 and 255.*got 1 other level\(s\) between 128 and 128"):
        prepare(step(Route.MASK, [base], encoded(grey), Size(64, 64)), 32)


def test_unbinarised_rgba_mask_is_rejected_not_repainted_whole() -> None:
    base = encoded(Image.new("RGB", (64, 64)))
    strokes = Image.new("RGBA", (64, 64), (255, 255, 255, 0))  # white but transparent: would read as "repaint all"
    strokes.paste((255, 255, 255, 255), (0, 0, 8, 8))
    with pytest.raises(ValueError, match="got mode RGBA"):
        prepare(step(Route.MASK, [base], encoded(strokes), Size(64, 64)), 32)


def test_mask_of_another_size_fails_loudly() -> None:
    base = encoded(Image.new("RGB", (960, 540)))
    mask = encoded(striped_mask(960, 544))
    with pytest.raises(ValueError, match=r"mask must be the base image's size 960x540, got 960x544"):
        prepare(step(Route.MASK, [base], mask), 32)


def test_output_size_must_fit_the_multiple() -> None:
    with pytest.raises(ValueError, match="multiple of 32"):
        prepare(step(Route.T2I, size=Size(1000, 700)), 32)


def test_capabilities() -> None:
    caps = ComfyBackend(ComfyConfig(base_url=UNREACHABLE_URL)).capabilities()
    assert caps.routes == frozenset(Route)
    assert caps.max_images >= 4 and caps.size_multiple == 32 and caps.max_seed == 2**64 - 1


def test_config_addresses() -> None:
    config = ComfyConfig(base_url="http://10.0.0.5:9000")
    assert config.host == "10.0.0.5:9000"
    assert config.ws_url == "ws://10.0.0.5:9000/ws"
    assert ComfyConfig().base_url == "http://127.0.0.1:8188"


@pytest.mark.parametrize("prefix", ["", "image*", "img?", "img[1]", "a/b", "a\\b", "../x"])
def test_file_prefix_with_glob_or_path_characters_is_refused(prefix: str) -> None:
    with pytest.raises(ValueError, match="file_prefix must be"):
        ComfyConfig(file_prefix=prefix)


def test_cleanup_removes_only_this_runs_files(tmp_path) -> None:
    config = ComfyConfig(root=tmp_path)
    config.input_dir.mkdir()
    config.output_dir.mkdir()
    mine = [config.input_dir / "imageapi_abc123_image1.png", config.output_dir / "imageapi_abc123_out_00001_.png"]
    others = [
        config.input_dir / "imageapi_abc1234_image1.png",  # a token that merely starts with this one
        config.input_dir / "imageapi_def456_image1.png",  # another run
        config.input_dir / "example.png",
        config.output_dir / "imageapi_def456_out_00001_.png",  # another run
        config.output_dir / "imageapi_abc1234_out_00001_.png",
        config.output_dir / "ComfyUI_00001_.png",
    ]
    for path in mine + others:
        path.write_bytes(b"x")
    removed = remove_run_files(config, "imageapi_abc123", uploaded=1, outputs=1)
    assert sorted(removed) == sorted(mine)
    assert all(p.exists() for p in others) and not any(p.exists() for p in mine)


def test_cleanup_finding_fewer_files_than_made_fails_loudly(tmp_path) -> None:
    config = ComfyConfig(root=tmp_path)
    with pytest.raises(CleanupError, match="expected 2 file"):
        remove_run_files(config, "imageapi_abc123", uploaded=2, outputs=0)


def test_cleanup_refuses_a_token_that_could_match_other_files(tmp_path) -> None:
    with pytest.raises(ValueError):
        remove_run_files(ComfyConfig(root=tmp_path), "imageapi_*", uploaded=0, outputs=0)


async def test_malformed_steps_fail_before_contacting_comfyui() -> None:
    backend = ComfyBackend(ComfyConfig(base_url=UNREACHABLE_URL))  # never contacted
    with pytest.raises(ValueError, match="route ref takes 1–16 images"):
        await backend.run(step(Route.REF))
    with pytest.raises(ValueError, match="seed must be in"):
        await backend.run(StepInput(Route.T2I, "x", (), None, -1, Size(64, 64)))
