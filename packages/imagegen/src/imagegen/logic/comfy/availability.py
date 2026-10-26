"""Route availability from ComfyUI's /object_info. The requirements are read off the very graphs that will run,
so there is no second list of nodes, inputs or files to keep in sync. Nothing on disk is scanned.
"""

from __future__ import annotations

from typing import Any

from ..backend import Unavailable, UnavailableCode, UnexpectedResponse
from ..types import Route, Size
from . import graphs
from .config import ComfyConfig

# Loader node → (file input, folder under the ComfyUI root, what the file is, for the message)
LOADER_FILES: dict[str, tuple[str, str, str]] = {
    "UNETLoader": ("unet_name", "models/diffusion_models", "底模"),
    "CLIPLoader": ("clip_name", "models/text_encoders", "文本编码器"),
    "VAELoader": ("vae_name", "models/vae", "VAE"),
    "ModelPatchLoader": ("name", "models/model_patches", "蒙版权重"),
}

_PROBE = {"prompt": "probe", "seed": 0, "size": Size(1024, 1024), "output_prefix": "probe"}
_PROBE_INPUTS: dict[Route, tuple[tuple[str, ...], str | None]] = {
    Route.T2I: ((), None),
    Route.REF: (("probe.png",), None),
    Route.MASK: (("probe.png",), "probe_mask.png"),
}


def check_routes(object_info: dict[str, Any], config: ComfyConfig) -> dict[Route, Unavailable | None]:
    return {route: check_route(object_info, config, route) for route in Route}


def check_route(object_info: dict[str, Any], config: ComfyConfig, route: Route) -> Unavailable | None:
    """None when every node, input, option and model file the route's graph uses is offered by this ComfyUI."""
    images, mask = _PROBE_INPUTS[route]
    graph = graphs.build(config, route, images=images, mask=mask, **_PROBE)
    problems: list[Unavailable] = []
    for node in graph.values():
        try:
            problems += _node_problems(node["class_type"], node["inputs"], object_info)
        except (AttributeError, TypeError, IndexError, KeyError) as exc:
            raw = str(object_info.get(node["class_type"]))[:2000]
            raise UnexpectedResponse(f"/object_info for {node['class_type']} has an unknown shape", raw) from exc
    if not problems:
        return None
    unique = list(dict.fromkeys(problems))
    return Unavailable(
        code=unique[0].code,
        message="；".join(p.message for p in unique),
        raw="\n".join(p.raw for p in unique),
    )


def _node_problems(class_type: str, inputs: dict[str, Any], object_info: dict[str, Any]) -> list[Unavailable]:
    info = object_info.get(class_type)
    if info is None:
        return [Unavailable(UnavailableCode.MISSING_NODE, f"缺少节点 {class_type}", class_type)]
    required: dict[str, Any] = info.get("input", {}).get("required", {})
    declared = {**required, **info.get("input", {}).get("optional", {})}
    problems: list[Unavailable] = []

    for key, value in inputs.items():
        name, _, slot = key.partition(".")
        spec = declared.get(name)
        if spec is None or (slot and slot not in _autogrow_names(spec)):
            problems.append(_missing_node(f"节点 {class_type} 缺少输入 {key}", f"{class_type}.{key}"))
            continue
        options = _combo_options(spec)
        if options is None or isinstance(value, list) or _is_upload(spec) or value in options:
            continue
        loader = LOADER_FILES.get(class_type)
        if loader is not None and loader[0] == name:
            _, folder, label = loader
            path = f"{folder}/{value}"
            problems.append(Unavailable(UnavailableCode.MISSING_FILE, f"缺少{label} {path}", path))
        else:
            problems.append(
                _missing_node(f"节点 {class_type} 的输入 {name} 不支持 {value}", f"{class_type}.{name}={value}")
            )

    for name, spec in required.items():
        if name not in inputs and not _autogrow_names(spec) and not any(k.startswith(f"{name}.") for k in inputs):
            problems.append(_missing_node(f"节点 {class_type} 需要输入 {name}，工作流里没有", f"{class_type}.{name}"))
    return problems


def _missing_node(message: str, raw: str) -> Unavailable:
    return Unavailable(UnavailableCode.MISSING_NODE, message, raw)


def _combo_options(spec: list[Any]) -> list[Any] | None:
    """Options of a combo input, in either object_info style: [[options…], {…}] or ["COMBO", {"options": […]}]."""
    if spec and isinstance(spec[0], list):
        return spec[0]
    if spec and spec[0] == "COMBO" and len(spec) > 1:
        return spec[1].get("options", [])
    return None


def _autogrow_names(spec: list[Any]) -> list[str]:
    if spec and isinstance(spec[0], str) and spec[0].startswith("COMFY_AUTOGROW") and len(spec) > 1:
        return spec[1].get("template", {}).get("names", [])
    return []


def _is_upload(spec: list[Any]) -> bool:
    """LoadImage-style inputs list the input folder; uploaded names are not in it until uploaded."""
    return len(spec) > 1 and isinstance(spec[1], dict) and bool(spec[1].get("image_upload"))
