"""Capability probes for suites. A missing capability = that suite is red, with the name stated; it is never silently skipped."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from collections.abc import Callable


def _comfy_url() -> str:
    # The ComfyUI address has exactly one source: the core package's ComfyUI config. The gate does not keep its own copy.
    from imagegen.logic.comfy.config import ComfyConfig

    return ComfyConfig().base_url


def _comfyui() -> str | None:
    try:
        url = _comfy_url()
    except Exception as exc:  # report the cause precisely
        return f"cannot read the ComfyUI address (imagegen.logic.comfy.config.ComfyConfig): {exc!r}"
    try:
        with urllib.request.urlopen(f"{url}/system_stats", timeout=5) as resp:
            json.load(resp)
    except Exception as exc:
        return f"ComfyUI unreachable {url}: {exc!r}"
    return None


def _comfyui_service() -> str | None:
    out = subprocess.run(["systemctl", "--user", "is-active", "comfyui.service"], capture_output=True, text=True)
    state = out.stdout.strip()
    return None if state == "active" else f"comfyui.service state={state or out.stderr.strip()}"


PROBES: dict[str, Callable[[], str | None]] = {"comfyui": _comfyui, "comfyui-service": _comfyui_service}


def missing(needs: tuple[str, ...]) -> list[str]:
    """Return the list of reasons for missing capabilities; an empty list means everything is present."""
    reasons = []
    for name in needs:
        reason = PROBES[name]()
        if reason:
            reasons.append(f"{name}: {reason}")
    return reasons
