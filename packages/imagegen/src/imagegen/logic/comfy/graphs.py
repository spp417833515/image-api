"""ComfyUI API-format graphs. Pure functions: files and sampling come from the model profile (Qwen via ComfyConfig).

Images are referenced by the names they were uploaded under. The backend sizes every image before uploading
(canvas = output size, references at multiples of 32). Qwen's encoder runs with resolution=0 and resizes nothing:
TextEncodeQwenImage21 shifts the edit when image_1 and the sampling latent differ in size.
FLUX 3 Klein encodes references through ReferenceMultiLatent at about 1 megapixel; the sampling latent stays the
output size the caller asked for.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from ..types import ModelId, Route, Size
from .catalog import ModelProfile, profile_for
from .config import ComfyConfig
from .rewriter import nodes as rewrite_nodes

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
    model: ModelId = ModelId.QWEN_IMAGE_2_1,
) -> Graph:
    """Graph for one step. images / mask are uploaded file names, in StepInput order."""
    spec = profile_for(config, model)
    common = {"prompt": prompt, "seed": seed, "size": size, "output_prefix": output_prefix, "spec": spec}
    match (spec.family, route):
        case ("qwen21", Route.T2I):
            _expect(route, images, mask, min_images=0, max_images=0, with_mask=False)
            return text_to_image(config, **common)
        case ("qwen21", Route.REF):
            _expect(route, images, mask, min_images=1, max_images=MAX_ENCODER_IMAGES, with_mask=False)
            return reference_edit(config, images=images, **common)
        case ("qwen21", Route.MASK):
            _expect(route, images, mask, min_images=1, max_images=MAX_ENCODER_IMAGES, with_mask=True)
            assert mask is not None
            return mask_rewrite(config, base=images[0], mask=mask, references=images[1:], **common)
        case ("flux3", Route.T2I):
            _expect(route, images, mask, min_images=0, max_images=0, with_mask=False)
            return flux_text_to_image(**common)
        case ("flux3", Route.REF):
            _expect(route, images, mask, min_images=1, max_images=MAX_ENCODER_IMAGES, with_mask=False)
            return flux_reference_edit(images=images, **common)
        case ("flux3", Route.MASK):
            _expect(route, images, mask, min_images=1, max_images=MAX_ENCODER_IMAGES, with_mask=True)
            assert mask is not None
            return flux_mask_rewrite(config, base=images[0], mask=mask, references=images[1:], **common)
        case _:
            raise ValueError(f"model {model} has no graph for route {route}")


def text_to_image(
    config: ComfyConfig, *, spec: ModelProfile, prompt: str, seed: int, size: Size, output_prefix: str
) -> Graph:
    graph = _loaders(spec)
    graph.update(
        _sample_and_save(
            config, spec, prompt, seed, size, output_prefix, model=["unet", 0], encoder_images=[], rewriter="t2i"
        )
    )
    return graph


def reference_edit(
    config: ComfyConfig,
    *,
    spec: ModelProfile,
    prompt: str,
    seed: int,
    size: Size,
    images: Sequence[str],
    output_prefix: str,
) -> Graph:
    """images[0] is the canvas and must already be `size`; the rest are references.

    The edit rewriter sees those same images, in that order, before the image model does.
    """
    graph = _loaders(spec)
    loads = _load_images(graph, images)
    graph.update(
        _sample_and_save(
            config,
            spec,
            prompt,
            seed,
            size,
            output_prefix,
            model=["unet", 0],
            encoder_images=loads,
            rewriter="edit",
            edit_images=loads,
        )
    )
    return graph


def mask_rewrite(
    config: ComfyConfig,
    *,
    spec: ModelProfile,
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
    graph = _loaders(spec)
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
            config,
            spec,
            prompt,
            seed,
            size,
            output_prefix,
            model=["unet", 0],
            encoder_images=loads,
            latent=["noise_mask", 0],
            rewriter="edit",
            edit_images=loads,
        )
    )
    return graph


def flux_text_to_image(*, spec: ModelProfile, prompt: str, seed: int, size: Size, output_prefix: str) -> Graph:
    graph = _loaders(spec)
    graph.update(_flux_sample(spec, prompt, seed, size, output_prefix, references=[]))
    return graph


def flux_reference_edit(
    *, spec: ModelProfile, prompt: str, seed: int, size: Size, images: Sequence[str], output_prefix: str
) -> Graph:
    """images[0] is the canvas and must already be `size`; the rest are references."""
    graph = _loaders(spec)
    loads = _load_images(graph, images)
    graph.update(_flux_sample(spec, prompt, seed, size, output_prefix, references=loads))
    return graph


def flux_mask_rewrite(
    config: ComfyConfig,
    *,
    spec: ModelProfile,
    prompt: str,
    seed: int,
    size: Size,
    base: str,
    mask: str,
    references: Sequence[str],
    output_prefix: str,
) -> Graph:
    """Repaint the white area, same latent-mask idea as the Qwen route, with the base also sent as a reference."""
    graph = _loaders(spec)
    graph["load_base"] = {"class_type": "LoadImage", "inputs": {"image": base}}
    graph["load_mask"] = {"class_type": "LoadImageMask", "inputs": {"image": mask, "channel": config.mask_channel}}
    graph["base_latent"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["load_base", 0], "vae": ["vae", 0]}}
    graph["noise_mask"] = {
        "class_type": "SetLatentNoiseMask",
        "inputs": {"samples": ["base_latent", 0], "mask": ["load_mask", 0]},
    }
    loads = ["load_base", *_load_images(graph, references, first=2)]
    graph.update(_flux_sample(spec, prompt, seed, size, output_prefix, references=loads, latent=["noise_mask", 0]))
    return graph


def _flux_sample(
    spec: ModelProfile,
    prompt: str,
    seed: int,
    size: Size,
    output_prefix: str,
    *,
    references: Sequence[str],
    latent: list[Any] | None = None,
) -> Graph:
    """CLIP text, optional reference conditioning, Flux 2 latent, sampler, decoder, save."""
    nodes: Graph = {
        "encode": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["encode", 0]}},
    }
    positive: list[Any] = ["encode", 0]
    negative: list[Any] = ["neg", 0]
    if references:
        ref_inputs: dict[str, Any] = {
            "positive": positive,
            "negative": negative,
            "vae": ["vae", 0],
            "resolution": "resize",
            "resolution.megapixels": 1.0,
        }
        for index, node_id in enumerate(references):
            ref_inputs[f"images.image{index}"] = [node_id, 0]
        nodes["refs"] = {"class_type": "ReferenceMultiLatent", "inputs": ref_inputs}
        positive, negative = ["refs", 0], ["refs", 1]
    if latent is None:
        nodes["latent"] = {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": size.width, "height": size.height, "batch_size": 1},
        }
        latent = ["latent", 0]
    return nodes | {
        SAMPLER: {
            "class_type": "KSampler",
            "inputs": {
                "model": ["unet", 0],
                "positive": positive,
                "negative": negative,
                "latent_image": latent,
                "seed": seed,
                "steps": spec.steps,
                "cfg": spec.cfg,
                "sampler_name": spec.sampler,
                "scheduler": spec.scheduler,
                "denoise": spec.denoise,
            },
        },
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": [SAMPLER, 0], "vae": ["vae", 0]}},
        SAVE: {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": output_prefix}},
    }


def _loaders(spec: ModelProfile) -> Graph:
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": spec.unet, "weight_dtype": "default"}},
        "clip": {
            "class_type": "CLIPLoader",
            "inputs": {"clip_name": spec.clip, "type": spec.clip_type, "device": "default"},
        },
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": spec.vae}},
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
    spec: ModelProfile,
    prompt: str,
    seed: int,
    size: Size,
    output_prefix: str,
    *,
    model: list[Any],
    encoder_images: Sequence[str],
    latent: list[Any] | None = None,
    rewriter: Literal["t2i", "edit"],
    edit_images: Sequence[str] = (),
) -> Graph:
    """Qwen encoder, sampler, decoder and output. latent: where sampling starts; None = an empty latent of `size`.

    The rewriter runs first and its paragraph is what the image model reads. Text-to-image uses the text-only
    checkpoint. Edits use the vision checkpoint and must be shown the same images the encoder gets, in the same order.
    """
    nodes = rewrite_nodes(config, prompt, seed, kind=rewriter, edit_images=edit_images)
    encoded_prompt: list[Any] = ["rewrite", 0]
    encode_inputs: dict[str, Any] = {
        "clip": ["clip", 0],
        "prompt": encoded_prompt,
        "negative_prompt": "",
        "vae": ["vae", 0],
        "resolution": 0,
    }
    for slot, node_id in enumerate(encoder_images, start=1):
        encode_inputs[f"images.image_{slot}"] = [node_id, 0]
    nodes["encode"] = {"class_type": "TextEncodeQwenImage21", "inputs": encode_inputs}
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
                "steps": spec.steps,
                "cfg": spec.cfg,
                "sampler_name": spec.sampler,
                "scheduler": spec.scheduler,
                "denoise": spec.denoise,
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
