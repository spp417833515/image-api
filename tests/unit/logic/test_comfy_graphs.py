"""The three ComfyUI graphs: key nodes, how they are wired, and that model files / sampling come from ComfyConfig."""

from __future__ import annotations

from typing import Any

import pytest
from imagegen.logic.comfy import graphs
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.types import Route, Size

CONFIG = ComfyConfig()


def build(route: Route, images: tuple[str, ...] = (), mask: str | None = None, config: ComfyConfig = CONFIG):
    return graphs.build(
        config, route, prompt="p", seed=9, size=Size(960, 544), images=images, mask=mask, output_prefix="imageapi_t_out"
    )


def nodes_of(graph: graphs.Graph, class_type: str) -> dict[str, dict[str, Any]]:
    return {node_id: node for node_id, node in graph.items() if node["class_type"] == class_type}


def assert_links_resolve(graph: graphs.Graph) -> None:
    for node_id, node in graph.items():
        for key, value in node["inputs"].items():
            if isinstance(value, list):
                assert value[0] in graph, f"{node_id}.{key} links to missing node {value[0]}"


def assert_starts_from_an_empty_latent(graph: graphs.Graph) -> None:
    start = graph[graph[graphs.SAMPLER]["inputs"]["latent_image"][0]]
    assert start == {"class_type": "EmptyLatentImage", "inputs": {"width": 960, "height": 544, "batch_size": 1}}


def assert_common_tail(graph: graphs.Graph, model_from: str) -> None:
    sample = graph[graphs.SAMPLER]
    assert sample["class_type"] == "KSampler"
    assert sample["inputs"]["model"] == [model_from, 0]
    assert sample["inputs"]["positive"] == ["encode", 0]
    assert sample["inputs"]["negative"] == ["encode", 1]
    assert {k: sample["inputs"][k] for k in ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise")} == {
        "seed": 9,
        "steps": 25,
        "cfg": 1.0,
        "sampler_name": "euler",
        "scheduler": "simple",
        "denoise": 1.0,
    }
    assert graph["decode"]["inputs"] == {"samples": [graphs.SAMPLER, 0], "vae": ["vae", 0]}
    assert graph[graphs.SAVE] == {
        "class_type": "SaveImage",
        "inputs": {"images": ["decode", 0], "filename_prefix": "imageapi_t_out"},
    }
    encode = graph["encode"]
    assert encode["class_type"] == "TextEncodeQwenImage21"
    assert encode["inputs"]["clip"] == ["clip", 0] and encode["inputs"]["vae"] == ["vae", 0]
    assert_links_resolve(graph)


def test_text_to_image() -> None:
    graph = build(Route.T2I)
    assert graph["unet"]["inputs"]["unet_name"] == "qwen-image-2.1-UC-int8_convrot.safetensors"
    assert graph["clip"]["inputs"] == {
        "clip_name": "qwen3vl_8b_int8_convrot.safetensors",
        "type": "qwen_image",
        "device": "default",
    }
    assert graph["vae"]["inputs"]["vae_name"] == "qwen_image_2.1_vae_bf16.safetensors"
    for absent in ("LoadImage", "LoadImageMask", "VAEEncode", "SetLatentNoiseMask"):
        assert not nodes_of(graph, absent), absent
    assert not [k for k in graph["encode"]["inputs"] if k.startswith("images.")]
    assert graph["encode"]["inputs"]["prompt"] == "p" and graph["encode"]["inputs"]["negative_prompt"] == ""
    assert_starts_from_an_empty_latent(graph)
    assert_common_tail(graph, model_from="unet")


def test_reference_edit_feeds_every_image_to_the_encoder_in_order() -> None:
    graph = build(Route.REF, images=("a.png", "b.png", "c.png"))
    encode = graph["encode"]["inputs"]
    slots = {k: v for k, v in encode.items() if k.startswith("images.")}
    assert list(slots) == ["images.image_1", "images.image_2", "images.image_3"]
    assert [graph[link[0]]["inputs"]["image"] for link in slots.values()] == ["a.png", "b.png", "c.png"]
    assert all(graph[link[0]]["class_type"] == "LoadImage" for link in slots.values())
    assert encode["resolution"] == 0  # images arrive pre-sized; image_1 must stay the latent's size
    assert_starts_from_an_empty_latent(graph)
    assert_common_tail(graph, model_from="unet")


def test_mask_rewrite_samples_from_the_base_latent_under_the_mask() -> None:
    graph = build(Route.MASK, images=("base.png", "ref.png"), mask="mask.png")
    for gone in ("ModelPatchLoader", "ZImageFunControlnet", "QwenImageDiffsynthControlnet", "EmptyLatentImage"):
        assert not nodes_of(graph, gone), gone
    sample = graph[graphs.SAMPLER]["inputs"]
    noise_mask = graph[sample["latent_image"][0]]
    assert sample["latent_image"][1] == 0 and noise_mask["class_type"] == "SetLatentNoiseMask"
    start = graph[noise_mask["inputs"]["samples"][0]]
    assert noise_mask["inputs"]["samples"][1] == 0 and start["class_type"] == "VAEEncode"
    assert start["inputs"]["vae"] == ["vae", 0]
    assert start["inputs"]["pixels"][1] == 0
    assert graph[start["inputs"]["pixels"][0]] == {"class_type": "LoadImage", "inputs": {"image": "base.png"}}
    assert noise_mask["inputs"]["mask"][1] == 0
    assert graph[noise_mask["inputs"]["mask"][0]] == {
        "class_type": "LoadImageMask",
        "inputs": {"image": "mask.png", "channel": "red"},
    }
    encode = graph["encode"]["inputs"]
    assert encode["images.image_1"] == start["inputs"]["pixels"]  # the base is also the encoder's image_1
    assert graph[encode["images.image_2"][0]]["inputs"]["image"] == "ref.png"
    assert "images.image_3" not in encode
    assert_common_tail(graph, model_from="unet")


def test_model_files_and_sampling_come_from_config() -> None:
    config = ComfyConfig(unet="u.safetensors", vae="v.safetensors", steps=30, sampler="dpmpp_2m", mask_channel="green")
    graph = build(Route.MASK, images=("base.png",), mask="m.png", config=config)
    assert graph["unet"]["inputs"]["unet_name"] == "u.safetensors"
    assert graph["vae"]["inputs"]["vae_name"] == "v.safetensors"
    assert nodes_of(graph, "LoadImageMask")["load_mask"]["inputs"]["channel"] == "green"
    assert graph[graphs.SAMPLER]["inputs"]["steps"] == 30
    assert graph[graphs.SAMPLER]["inputs"]["sampler_name"] == "dpmpp_2m"


@pytest.mark.parametrize(
    ("route", "images", "mask"),
    [
        (Route.T2I, ("a.png",), None),
        (Route.T2I, (), "m.png"),
        (Route.REF, (), None),
        (Route.REF, ("a.png",), "m.png"),
        (Route.MASK, ("a.png",), None),
        (Route.MASK, (), "m.png"),
        (Route.REF, tuple(f"{n}.png" for n in range(17)), None),
    ],
)
def test_wrong_inputs_for_a_route_fail_loudly(route: Route, images: tuple[str, ...], mask: str | None) -> None:
    with pytest.raises(ValueError, match=f"route {route} takes"):
        build(route, images=images, mask=mask)
