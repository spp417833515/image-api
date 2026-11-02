"""The three ComfyUI graphs: key nodes, how they are wired, and that model files / sampling come from ComfyConfig."""

from __future__ import annotations

from typing import Any

import pytest
from imagegen.logic.comfy import graphs
from imagegen.logic.comfy.config import ComfyConfig
from imagegen.logic.types import ModelId, Route, Size

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
    assert graph["encode"]["inputs"]["prompt"] == ["rewrite", 0]
    assert graph["encode"]["inputs"]["negative_prompt"] == ""
    assert graph["rewrite"]["inputs"]["prompt"] == "p"
    assert graph["rewrite"]["inputs"]["thinking"] is True
    assert graph["rewrite"]["inputs"]["sampling_mode.presence_penalty"] == 1.5
    assert "image" not in graph["rewrite"]["inputs"]
    assert "pe_batch" not in graph
    assert graph["pe"]["inputs"] == {
        "clip_name": "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
        "type": "stable_diffusion",
        "device": "default",
    }
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
    assert encode["prompt"] == ["rewrite", 0]
    assert graph["rewrite"]["inputs"]["prompt"] == "p"
    assert graph["rewrite"]["inputs"]["sampling_mode.presence_penalty"] == 0.0
    assert graph["rewrite"]["inputs"]["system_prompt"].startswith("# Edit Prompt Enhancer")
    assert graph["pe"]["inputs"] == {
        "clip_name": "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
        "type": "qwen_image",
        "device": "default",
    }
    assert graph["rewrite"]["inputs"]["image"] == ["pe_batch", 0]
    batch = graph["pe_batch"]["inputs"]
    assert list(batch) == ["images.image0", "images.image1", "images.image2"]
    assert [graph[link[0]]["inputs"]["image"] for link in batch.values()] == ["a.png", "b.png", "c.png"]
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
    # The rewriter sees the base and the reference, not the mask.
    assert graph["rewrite"]["inputs"]["image"] == ["pe_batch", 0]
    assert list(graph["pe_batch"]["inputs"]) == ["images.image0", "images.image1"]
    assert graph["pe_batch"]["inputs"]["images.image0"] == ["load_base", 0]
    assert graph[graph["pe_batch"]["inputs"]["images.image1"][0]]["inputs"]["image"] == "ref.png"
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


def flux(route: Route, images: tuple[str, ...] = (), mask: str | None = None):
    return graphs.build(
        CONFIG,
        route,
        prompt="p",
        seed=9,
        size=Size(960, 544),
        images=images,
        mask=mask,
        output_prefix="imageapi_t_out",
        model=ModelId.FLUX3_KLEIN,
    )


def test_flux3_klein_text_to_image_uses_the_distilled_int8_files() -> None:
    graph = flux(Route.T2I)
    assert graph["unet"]["inputs"]["unet_name"] == "flux3-image-klein-int8-convrot.safetensors"
    assert graph["clip"]["inputs"] == {
        "clip_name": "qwen3vl_4b_int8_convrot.safetensors",
        "type": "flux3",
        "device": "default",
    }
    assert graph["vae"]["inputs"]["vae_name"] == "flux2-vae.safetensors"
    assert graph["encode"]["class_type"] == "CLIPTextEncode"
    assert graph["encode"]["inputs"]["text"] == "p"
    assert graph["latent"]["class_type"] == "EmptyFlux2LatentImage"
    assert graph["latent"]["inputs"] == {"width": 960, "height": 544, "batch_size": 1}
    sample = graph[graphs.SAMPLER]["inputs"]
    assert sample["positive"] == ["encode", 0] and sample["negative"] == ["neg", 0]
    assert sample["steps"] == 16 and sample["cfg"] == 1.0
    assert sample["sampler_name"] == "euler" and sample["scheduler"] == "simple"
    assert not nodes_of(graph, "TextEncodeQwenImage21")
    assert "rewrite" not in graph
    assert_links_resolve(graph)


def test_flux3_klein_reference_and_mask_keep_the_output_size() -> None:
    edited = flux(Route.REF, images=("a.png", "b.png"))
    assert edited["refs"]["inputs"]["resolution"] == "resize"
    assert edited["refs"]["inputs"]["resolution.megapixels"] == 1.0
    assert edited["refs"]["inputs"]["images.image0"][0] == "load_1"
    assert edited["refs"]["inputs"]["images.image1"][0] == "load_2"
    assert edited[graphs.SAMPLER]["inputs"]["latent_image"] == ["latent", 0]
    assert edited["latent"]["class_type"] == "EmptyFlux2LatentImage"
    masked = flux(Route.MASK, images=("base.png",), mask="mask.png")
    assert masked[graphs.SAMPLER]["inputs"]["latent_image"][0] == "noise_mask"
    assert masked["refs"]["inputs"]["images.image0"][0] == "load_base"
    assert "rewrite" not in edited and "rewrite" not in masked
    assert_links_resolve(edited)
    assert_links_resolve(masked)
