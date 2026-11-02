"""Route availability from ComfyUI's /object_info. The requirements are read off the very graphs that will run,
so there is no second list of nodes, inputs or files to keep in sync. Nothing on disk is scanned.
"""

from __future__ import annotations

from typing import Any

from ..backend import Unavailable, UnavailableCode, UnexpectedResponse
from ..types import ModelId, Route, Size
from . import graphs
from .config import ComfyConfig

# Loader node → (file input, folder under the ComfyUI root, what the file is, for the message)
LOADER_FILES: dict[str, tuple[str, str, str]] = {
    "UNETLoader": ("unet_name", "models/diffusion_models", "底模"),
    "CLIPLoader": ("clip_name", "models/text_encoders", "文本编码器"),
    "VAELoader": ("vae_name", "models/vae", "VAE"),
}

_PROBE = {"prompt": "probe", "seed": 0, "size": Size(1024, 1024), "output_prefix": "probe"}
_PROBE_INPUTS: dict[Route, tuple[tuple[str, ...], str | None]] = {
    Route.T2I: ((), None),
    Route.REF: (("probe.png",), None),
    Route.MASK: (("probe.png",), "probe_mask.png"),
}


def check_routes(
    object_info: dict[str, Any], config: ComfyConfig, model: ModelId = ModelId.QWEN_IMAGE_2_1
) -> dict[Route, Unavailable | None]:
    return {route: check_route(object_info, config, route, model) for route in Route}


def check_route(
    object_info: dict[str, Any], config: ComfyConfig, route: Route, model: ModelId = ModelId.QWEN_IMAGE_2_1
) -> Unavailable | None:
    """None when every node, input, option and model file the route's graph uses is offered by this ComfyUI."""
    images, mask = _PROBE_INPUTS[route]
    graph = graphs.build(config, route, images=images, mask=mask, model=model, **_PROBE)
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
        if spec is None or (slot and not _slot_known(spec, inputs.get(name), slot)):
            problems.append(_missing_node(f"节点 {class_type} 缺少输入 {key}", f"{class_type}.{key}"))
            continue
        if slot:
            spec = _slot_spec(spec, inputs.get(name), slot)
        elif spec and spec[0] == "COMFY_DYNAMICCOMBO_V3":
            keys = [option.get("key") for option in spec[1].get("options", [])]
            if value not in keys:
                problems.append(
                    _missing_node(f"节点 {class_type} 的输入 {name} 不支持 {value}", f"{class_type}.{name}={value}")
                )
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
    if not (spec and isinstance(spec[0], str) and spec[0].startswith("COMFY_AUTOGROW") and len(spec) > 1):
        return []
    template = spec[1].get("template", {})
    if "names" in template:
        return list(template["names"])
    prefix = template.get("prefix")
    if prefix is None:
        return []
    return [f"{prefix}{i}" for i in range(int(template.get("max", 0)))]


def _slot_known(spec: list[Any], selected: Any, slot: str) -> bool:
    return slot in _autogrow_names(spec) or _dynamic_child(spec, selected, slot) is not None


def _slot_spec(spec: list[Any], selected: Any, slot: str) -> list[Any]:
    """The nested input a dotted key writes, or the parent when the slot is only an autogrow name."""
    child = _dynamic_child(spec, selected, slot)
    return spec if child is None else child


def _dynamic_child(spec: list[Any], selected: Any, slot: str) -> list[Any] | None:
    if not (spec and spec[0] == "COMFY_DYNAMICCOMBO_V3" and len(spec) > 1):
        return None
    for option in spec[1].get("options", []):
        if option.get("key") != selected:
            continue
        fields = option.get("inputs", {})
        for group in ("required", "optional"):
            child = fields.get(group, {}).get(slot)
            if child is not None:
                return child
    return None


def _is_upload(spec: list[Any]) -> bool:
    """LoadImage-style inputs list the input folder; uploaded names are not in it until uploaded."""
    return len(spec) > 1 and isinstance(spec[1], dict) and bool(spec[1].get("image_upload"))
