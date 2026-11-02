"""One profile per image model: the ComfyUI files and sampling that profile's graph uses.

Qwen-Image 2.1 reads its files and sampler settings from ComfyConfig, so an existing install keeps working.
FLUX 3 Image Klein is the distilled INT8 build: it fits a 24 GB card. The bf16 weights and the 9B prompt
upsampler are not used; the instruction is encoded as written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..types import ModelId
from .config import ComfyConfig

Family = Literal["qwen21", "flux3"]


@dataclass(frozen=True, slots=True)
class ModelProfile:
    id: ModelId
    family: Family
    unet: str
    clip: str
    clip_type: str
    vae: str
    steps: int
    cfg: float
    sampler: str
    scheduler: str
    denoise: float


def profile_for(config: ComfyConfig, model: ModelId = ModelId.QWEN_IMAGE_2_1) -> ModelProfile:
    if model is ModelId.QWEN_IMAGE_2_1:
        return ModelProfile(
            id=model,
            family="qwen21",
            unet=config.unet,
            clip=config.clip,
            clip_type=config.clip_type,
            vae=config.vae,
            steps=config.steps,
            cfg=config.cfg,
            sampler=config.sampler,
            scheduler=config.scheduler,
            denoise=config.denoise,
        )
    if model is ModelId.FLUX3_KLEIN:
        return ModelProfile(
            id=model,
            family="flux3",
            unet="flux3-image-klein-int8-convrot.safetensors",
            clip="qwen3vl_4b_int8_convrot.safetensors",
            clip_type="flux3",
            vae="flux2-vae.safetensors",
            steps=16,
            cfg=1.0,
            sampler="euler",
            scheduler="simple",
            denoise=1.0,
        )
    raise ValueError(f"unknown model {model!r}")
