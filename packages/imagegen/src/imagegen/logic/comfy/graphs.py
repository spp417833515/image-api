"""The three ComfyUI API-format graphs. Pure functions: model files and sampling parameters come only from ComfyConfig.

Images are referenced by the names they were uploaded under. The backend sizes every image before uploading
(canvas = output size, references at multiples of 32), so the encoder runs with resolution=0 and resizes nothing:
TextEncodeQwenImage21 shifts the edit when image_1 and the sampling latent differ in size.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..types import Route, Size
from .config import ComfyConfig

type Graph = dict[str, dict[str, Any]]

SAMPLER = "sample"  # node id whose progress is the step's progress
SAVE = "save"  # node id of the only output node
MAX_ENCODER_IMAGES = 16  # TextEncodeQwenImage21 slots images.image_1 … images.image_16


def build(
    config: ComfyConfig,
    route: Route,
    *,
    prompt: str,
    seed: int,
    size: Size,
    images: Sequence[str],
    mask: str | None,
    output_prefix: str,
) -> Graph:
    """Graph for one step. images / mask are uploaded file names, in StepInput order."""
    common = {"prompt": prompt, "seed": seed, "size": size, "output_prefix": output_prefix}
    match route:
        case Route.T2I:
            _expect(route, images, mask, min_images=0, max_images=0, with_mask=False)
            return text_to_image(config, **common)
        case Route.REF:
            _expect(route, images, mask, min_images=1, max_images=MAX_ENCODER_IMAGES, with_mask=False)
            return reference_edit(config, images=images, **common)
        case Route.MASK:
            _expect(route, images, mask, min_images=1, max_images=MAX_ENCODER_IMAGES, with_mask=True)
            assert mask is not None
            return mask_rewrite(config, base=images[0], mask=mask, references=images[1:], **common)


def text_to_image(config: ComfyConfig, *, prompt: str, seed: int, size: Size, output_prefix: str) -> Graph:
    graph = _loaders(config)
    graph.update(_sample_and_save(config, prompt, seed, size, output_prefix, model=["unet", 0], encoder_images=[]))
    return graph


def reference_edit(
    config: ComfyConfig, *, prompt: str, seed: int, size: Size, images: Sequence[str], output_prefix: str
) -> Graph:
    """images[0] is the canvas and must already be `size`; the rest are references."""
    graph = _loaders(config)
    loads = _load_images(graph, images)
    graph.update(_sample_and_save(config, prompt, seed, size, output_prefix, model=["unet", 0], encoder_images=loads))
    return graph


def mask_rewrite(
    config: ComfyConfig,
    *,
    prompt: str,
    seed: int,
    size: Size,
    base: str,
    mask: str,
    references: Sequence[str],
    output_prefix: str,
) -> Graph:
    """Repaint the white area of mask on base; base and mask must already be `size` (mask binary, white = repaint).

    Sampling starts from the base image's own latent (VAEEncode) with mask as its noise mask (SetLatentNoiseMask):
    every step puts the original latent back outside the white area, so only the white area is generated, in the
    context of the colours around it. The base also goes to the encoder as image_1, so the instruction can refer to
    what is under the mask ("make its fur blue" keeps the subject's pose). ppwiki decision id=5, fourth revision.

    Measured against the inpaint patch it replaces (ZImageFunControlnet + ModelPatchLoader, empty latent start), same
    seeds, .pp/tmp/line2/mask_rca: background shift inside the white area on solid colour 1.0–1.2/255 (was
    4.8–13.2, a visible grey square); 1 px and 8 px strokes come back as the original (were the patch's mid-grey fill,
    which its 16 px keep map missed); outside the white area 0.6–2.0/255 (was 1.2–7.2); edits of what is under the
    mask as strong as before or stronger; about 2.2x faster (8–17 s a step instead of 20–37 s). Known weakness: a
    small object asked for on a featureless flat colour is often not drawn (the area stays as it was).
    The business layer still pastes the original back outside the white area (imagegen.biz.masking.paste_back).
    """
    graph = _loaders(config)
    graph["load_base"] = {"class_type": "LoadImage", "inputs": {"image": base}}
    graph["load_mask"] = {"class_type": "LoadImageMask", "inputs": {"image": mask, "channel": config.mask_channel}}
    graph["base_latent"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["load_base", 0], "vae": ["vae", 0]}}
    graph["noise_mask"] = {
        "class_type": "SetLatentNoiseMask",
        "inputs": {"samples": ["base_latent", 0], "mask": ["load_mask", 0]},
    }
    loads = ["load_base", *_load_images(graph, references, first=2)]  # base as image_1: see the docstring
    graph.update(
        _sample_and_save(
            config, prompt, seed, size, output_prefix, model=["unet", 0], encoder_images=loads, latent=["noise_mask", 0]
        )
    )
    return graph


def _loaders(config: ComfyConfig) -> Graph:
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": config.unet, "weight_dtype": "default"}},
        "clip": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": config.clip, "type": config.clip_type, "device": "default"},
        },
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": config.vae}},
    }


def _load_images(graph: Graph, names: Sequence[str], first: int = 1) -> list[str]:
    node_ids = []
    for number, name in enumerate(names, start=first):
        node_id = f"load_{number}"
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
        node_ids.append(node_id)
    return node_ids


def _sample_and_save(
    config: ComfyConfig,
    prompt: str,
    seed: int,
    size: Size,
    output_prefix: str,
    *,
    model: list[Any],
    encoder_images: Sequence[str],
    latent: list[Any] | None = None,
) -> Graph:
    """Encoder, sampler, decoder and output node. latent: where sampling starts; None = an empty latent of `size`."""
    encode_inputs: dict[str, Any] = {
        "clip": ["clip", 0],
        "prompt": prompt,
        "negative_prompt": config.negative_prompt,
        "vae": ["vae", 0],
        "resolution": 0,
    }
    for slot, node_id in enumerate(encoder_images, start=1):
        encode_inputs[f"images.image_{slot}"] = [node_id, 0]
    nodes: Graph = {"encode": {"class_type": "TextEncodeQwenImage21", "inputs": encode_inputs}}
    if latent is None:
        nodes["latent"] = {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": size.width, "height": size.height, "batch_size": 1},
        }
        latent = ["latent", 0]
    return nodes | {
        SAMPLER: {
            "class_type": "KSampler",
            "inputs": {
                "model": model,
                "positive": ["encode", 0],
                "negative": ["encode", 1],
                "latent_image": latent,
                "seed": seed,
                "steps": config.steps,
                "cfg": config.cfg,
                "sampler_name": config.sampler,
                "scheduler": config.scheduler,
                "denoise": config.denoise,
            },
        },
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": [SAMPLER, 0], "vae": ["vae", 0]}},
        SAVE: {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": output_prefix}},
    }


def _expect(
    route: Route, images: Sequence[str], mask: str | None, *, min_images: int, max_images: int, with_mask: bool
) -> None:
    if not min_images <= len(images) <= max_images or (mask is not None) != with_mask:
        raise ValueError(
            f"route {route} takes {min_images}–{max_images} images and {'a' if with_mask else 'no'} mask; "
            f"got {len(images)} images and {'a' if mask is not None else 'no'} mask"
        )
