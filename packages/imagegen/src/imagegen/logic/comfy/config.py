"""The single source for how this project talks to ComfyUI: address, install location, model files, sampling parameters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# Cleanup finds a run's files by glob, so the prefix must not contain glob syntax or leave the directory.
_FORBIDDEN_IN_PREFIX = set("*?[]/\\")


@dataclass(frozen=True, slots=True)
class ComfyConfig:
    base_url: str = "http://127.0.0.1:8188"
    # The directory ComfyUI runs from. ComfyUI has no HTTP API to delete files, so the backend removes what a run
    # uploaded / generated straight from <root>/input and <root>/output: ComfyUI must run on this machine with its
    # default input and output directories. A remote ComfyUI is not supported (cleanup would fail loudly).
    root: Path = field(default_factory=lambda: Path.home() / "ComfyUI")
    # Every file this project uploads to or generates in ComfyUI starts with this, so cleanup never touches other files.
    file_prefix: str = "imageapi_"

    # Model files (the UC int8 checkpoint is uncensored; qwen_image_2.1_int8_convrot.safetensors is a symlink to it).
    unet: str = "qwen-image-2.1-UC-int8_convrot.safetensors"
    clip: str = "qwen3vl_8b_int8_convrot.safetensors"
    clip_type: str = "qwen_image"
    vae: str = "qwen_image_2.1_vae_bf16.safetensors"
    model_patch: str = "qwen_image_2.1_fun_controlnet_union_int8_convrot.safetensors"

    # Sampling (fixed for every route).
    steps: int = 25
    cfg: float = 1.0
    sampler: str = "euler"
    scheduler: str = "simple"
    denoise: float = 1.0
    negative_prompt: str = ""
    # Mask route: ZImageFunControlnet strength, and which channel of the uploaded mask PNG LoadImageMask reads.
    control_strength: float = 1.0
    mask_channel: str = "red"

    # Seconds: one HTTP request; one whole step (queue wait + model load + sampling); waiting for ComfyUI to let go of
    # a prompt that was cancelled, timed out or lost its websocket.
    http_timeout_s: float = 60.0
    run_timeout_s: float = 1800.0
    stop_timeout_s: float = 120.0

    def __post_init__(self) -> None:
        if not self.file_prefix or _FORBIDDEN_IN_PREFIX & set(self.file_prefix):
            raise ValueError(
                f"file_prefix must be non-empty and free of glob characters and path separators (*?[]/\\), "
                f"got {self.file_prefix!r}"
            )

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc

    @property
    def ws_url(self) -> str:
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}/ws"

    @property
    def input_dir(self) -> Path:
        return self.root / "input"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"
