"""Route availability from a captured /object_info (ComfyUI 0.42.0, commit 0b05cd8), edited per case."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from imagegen.logic.backend import UnavailableCode, UnexpectedResponse
from imagegen.logic.comfy.availability import check_route, check_routes
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.types import ModelId, Route

SAMPLE = json.loads((Path(__file__).parent / "fixtures/object_info_0.42.0.json").read_text(encoding="utf-8"))
CONFIG = ComfyConfig()


@pytest.fixture
def info() -> dict[str, Any]:
    return copy.deepcopy(SAMPLE)


def test_captured_sample_has_every_route(info) -> None:
    assert check_routes(info, CONFIG) == {Route.T2I: None, Route.REF: None, Route.MASK: None}


def test_a_missing_shared_file_blocks_every_route(info) -> None:
    """A file every route loads blocks all three. The edit rewriter's own file is not one of those."""
    info["VAELoader"]["input"]["required"]["vae_name"][0].remove(CONFIG.vae)
    for route, problem in check_routes(info, CONFIG).items():
        assert problem is not None, route
        assert problem.code is UnavailableCode.MISSING_FILE
        assert problem.message == f"缺少VAE models/vae/{CONFIG.vae}"
        assert problem.raw == f"models/vae/{CONFIG.vae}"


def test_the_edit_rewriter_is_required_only_when_there_is_an_image(info) -> None:
    info["CLIPLoader"]["input"]["required"]["clip_name"][0].remove(CONFIG.prompt_enhancer_edit)
    routes = check_routes(info, CONFIG)
    assert routes[Route.T2I] is None
    for route in (Route.REF, Route.MASK):
        problem = routes[route]
        assert problem is not None and problem.code is UnavailableCode.MISSING_FILE
        assert problem.message == f"缺少文本编码器 models/text_encoders/{CONFIG.prompt_enhancer_edit}"
        assert problem.raw == f"models/text_encoders/{CONFIG.prompt_enhancer_edit}"


def test_missing_base_model_blocks_every_route_and_names_the_file(info) -> None:
    info["UNETLoader"]["input"]["required"]["unet_name"][0].remove(CONFIG.unet)
    for route, problem in check_routes(info, CONFIG).items():
        assert problem is not None, route
        assert problem.code is UnavailableCode.MISSING_FILE
        assert problem.raw == f"models/diffusion_models/{CONFIG.unet}"


def test_several_missing_files_are_all_listed(info) -> None:
    info["VAELoader"]["input"]["required"]["vae_name"][0].remove(CONFIG.vae)
    info["CLIPLoader"]["input"]["required"]["clip_name"][0].remove(CONFIG.clip)
    problem = check_route(info, CONFIG, Route.T2I)
    assert problem is not None
    assert f"models/text_encoders/{CONFIG.clip}" in problem.message
    assert f"models/vae/{CONFIG.vae}" in problem.message
    assert problem.raw.splitlines() == [f"models/text_encoders/{CONFIG.clip}", f"models/vae/{CONFIG.vae}"]


@pytest.mark.parametrize("node", ["VAEEncode", "SetLatentNoiseMask", "LoadImageMask"])
def test_a_missing_mask_node_blocks_only_the_mask_route(info, node: str) -> None:
    del info[node]
    routes = check_routes(info, CONFIG)
    assert routes[Route.T2I] is None and routes[Route.REF] is None
    problem = routes[Route.MASK]
    assert problem is not None
    assert problem.code is UnavailableCode.MISSING_NODE
    assert problem.message == f"缺少节点 {node}"
    assert problem.raw == node


def test_missing_load_image_blocks_both_image_routes(info) -> None:
    del info["LoadImage"]
    routes = check_routes(info, CONFIG)
    assert routes[Route.T2I] is None
    assert routes[Route.REF] is not None and routes[Route.REF].code is UnavailableCode.MISSING_NODE
    assert routes[Route.MASK] is not None and routes[Route.MASK].code is UnavailableCode.MISSING_NODE


def test_a_missing_noise_mask_input_is_a_missing_node(info) -> None:
    del info["SetLatentNoiseMask"]["input"]["required"]["mask"]
    problem = check_route(info, CONFIG, Route.MASK)
    assert problem is not None
    assert problem.code is UnavailableCode.MISSING_NODE
    assert problem.message == "节点 SetLatentNoiseMask 缺少输入 mask"


def test_encoder_without_image_slots_blocks_image_routes(info) -> None:
    del info["TextEncodeQwenImage21"]["input"]["required"]["images"]
    assert check_route(info, CONFIG, Route.T2I) is None
    problem = check_route(info, CONFIG, Route.REF)
    assert problem is not None
    assert problem.message == "节点 TextEncodeQwenImage21 缺少输入 images.image_1"


def test_unsupported_option_is_a_missing_node(info) -> None:
    info["CLIPLoader"]["input"]["required"]["type"][0].remove("qwen_image")
    problem = check_route(info, CONFIG, Route.T2I)
    assert problem is not None
    assert problem.code is UnavailableCode.MISSING_NODE
    assert problem.message == "节点 CLIPLoader 的输入 type 不支持 qwen_image"


def test_new_required_input_the_graph_does_not_set(info) -> None:
    info["KSampler"]["input"]["required"]["eta"] = ["FLOAT", {"default": 1.0}]
    problem = check_route(info, CONFIG, Route.T2I)
    assert problem is not None
    assert problem.code is UnavailableCode.MISSING_NODE
    assert "KSampler" in problem.message and "eta" in problem.message


def test_new_style_combo_spec_is_understood(info) -> None:
    options = info["UNETLoader"]["input"]["required"]["unet_name"][0]
    info["UNETLoader"]["input"]["required"]["unet_name"] = ["COMBO", {"options": options}]
    assert check_route(info, CONFIG, Route.T2I) is None
    info["UNETLoader"]["input"]["required"]["unet_name"] = ["COMBO", {"options": ["other.safetensors"]}]
    problem = check_route(info, CONFIG, Route.T2I)
    assert problem is not None and problem.code is UnavailableCode.MISSING_FILE


def _combo(options: list[str]) -> list:
    return [options, {}]


def test_flux3_klein_is_available_when_its_nodes_and_files_are_offered() -> None:
    info = {
        "UNETLoader": {"input": {"required": {
            "unet_name": _combo(["flux3-image-klein-int8-convrot.safetensors"]),
            "weight_dtype": _combo(["default"]),
        }}},
        "CLIPLoader": {"input": {"required": {
            "clip_name": _combo(["qwen3vl_4b_int8_convrot.safetensors"]),
            "type": _combo(["flux3"]),
        }, "optional": {"device": _combo(["default"])}}},
        "VAELoader": {"input": {"required": {"vae_name": _combo(["flux2-vae.safetensors"])}}},
        "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {}], "clip": ["CLIP", {}]}}},
        "ConditioningZeroOut": {"input": {"required": {"conditioning": ["CONDITIONING", {}]}}},
        "EmptyFlux2LatentImage": {"input": {"required": {
            "width": ["INT", {}], "height": ["INT", {}], "batch_size": ["INT", {}],
        }}},
        "KSampler": {"input": {"required": {
            "model": ["MODEL", {}], "seed": ["INT", {}], "steps": ["INT", {}], "cfg": ["FLOAT", {}],
            "sampler_name": _combo(["euler"]), "scheduler": _combo(["simple"]),
            "positive": ["CONDITIONING", {}], "negative": ["CONDITIONING", {}],
            "latent_image": ["LATENT", {}], "denoise": ["FLOAT", {}],
        }}},
        "VAEDecode": {"input": {"required": {"samples": ["LATENT", {}], "vae": ["VAE", {}]}}},
        "SaveImage": {"input": {"required": {"images": ["IMAGE", {}], "filename_prefix": ["STRING", {}]}}},
        "LoadImage": {"input": {"required": {"image": ["STRING", {"image_upload": True}]}}},
        "ReferenceMultiLatent": {"input": {
            "required": {
                "positive": ["CONDITIONING", {}],
                "resolution": ["COMFY_DYNAMICCOMBO_V3", {"options": [
                    {"key": "keep resolution", "inputs": {"required": {}}},
                    {"key": "resize", "inputs": {"required": {"megapixels": ["FLOAT", {"default": 1.0}]}}},
                ]}],
            },
            "optional": {
                "negative": ["CONDITIONING", {}],
                "vae": ["VAE", {}],
                "images": ["COMFY_AUTOGROW_V3", {"template": {
                    "prefix": "image", "min": 1, "max": 4,
                    "input": {"optional": {"image": ["IMAGE", {}]}},
                }}],
            },
        }},
    }
    assert check_routes(info, CONFIG, ModelId.FLUX3_KLEIN)[Route.T2I] is None
    assert check_routes(info, CONFIG, ModelId.FLUX3_KLEIN)[Route.REF] is None


@pytest.mark.parametrize("broken", [["not", "a", "node"], {"input": "nope"}, {"input": {"required": [1, 2]}}])
def test_object_info_of_an_unknown_shape_is_unexpected_response(info, broken) -> None:
    info["KSampler"] = broken
    with pytest.raises(UnexpectedResponse, match="/object_info for KSampler has an unknown shape") as caught:
        check_route(info, CONFIG, Route.T2I)
    assert caught.value.raw == str(broken)
