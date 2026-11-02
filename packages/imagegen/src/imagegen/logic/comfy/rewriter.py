"""The Qwen-Image 2.1 prompt rewriters that run before the image model.

Text-to-image uses the text-only checkpoint. Edits use the vision checkpoint and are shown the current
images. The system prompts are the plain paragraphs baked into the official ComfyUI templates, not the
JSON shape on the Hugging Face cards: the generated text is fed straight to the image encoder.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from .config import ComfyConfig

type Graph = dict[str, dict[str, Any]]

_PE_SYSTEM = (Path(__file__).with_name("qwen_pe_system.txt")).read_text(encoding="utf-8")
_PE_EDIT_SYSTEM = (Path(__file__).with_name("qwen_pe_i2i_system.txt")).read_text(encoding="utf-8")


def nodes(
    config: ComfyConfig,
    prompt: str,
    seed: int,
    *,
    kind: Literal["t2i", "edit"],
    edit_images: Sequence[str] = (),
) -> Graph:
    """TextGenerate plus the checkpoint it reads. edit_images are node ids already in the caller's graph."""
    editing = kind == "edit"
    if editing and not edit_images:
        raise ValueError("the edit rewriter needs the images it rewrites against")
    if not editing and edit_images:
        raise ValueError("the text-to-image rewriter does not take images")
    rewrite_inputs: dict[str, Any] = {
        "clip": ["pe", 0],
        "prompt": prompt,
        "system_prompt": _PE_EDIT_SYSTEM if editing else _PE_SYSTEM,
        "max_length": 4096,
        "sampling_mode": "on",
        "sampling_mode.temperature": 1.0,
        "sampling_mode.top_k": 20,
        "sampling_mode.top_p": 0.95,
        "sampling_mode.min_p": 0.0,
        "sampling_mode.repetition_penalty": 1.0,
        "sampling_mode.seed": seed,
        # Official templates: 1.5 for text-to-image, 0 for edits. Not the same knob.
        "sampling_mode.presence_penalty": 0.0 if editing else 1.5,
        "thinking": True,
        "use_default_template": True,
        "mtp": "auto",
    }
    graph: Graph = {
        "pe": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": config.prompt_enhancer_edit if editing else config.prompt_enhancer,
                "type": config.prompt_enhancer_edit_type if editing else config.prompt_enhancer_type,
                "device": "default",
            },
        }
    }
    if editing:
        # TextGenerate takes one image. The batch keeps order: image 0 is the canvas.
        graph["pe_batch"] = {
            "class_type": "BatchImagesNode",
            "inputs": {f"images.image{index}": [node_id, 0] for index, node_id in enumerate(edit_images)},
        }
        rewrite_inputs["image"] = ["pe_batch", 0]
    graph["rewrite"] = {"class_type": "TextGenerate", "inputs": rewrite_inputs}
    return graph
