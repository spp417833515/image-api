"""Follow one prompt through ComfyUI's websocket messages: report sampler progress, stop at the end, keep errors verbatim."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..backend import StepFailed, UnexpectedResponse
from ..types import ProgressCallback


class StreamEnded(Exception):
    """The websocket closed before ComfyUI said the prompt was finished."""


async def follow(
    events: AsyncIterator[dict[str, Any]],
    prompt_id: str,
    *,
    progress_node: str,
    on_progress: ProgressCallback | None,
) -> None:
    """Return when the prompt has finished successfully; raise StepFailed if ComfyUI reports an error or interruption,
    UnexpectedResponse if a message about this prompt cannot be read.

    ComfyUI's last message for a prompt is `executing` with node=None, sent after the history entry is written,
    so the history can be read as soon as this returns.
    """
    async for message in events:
        kind, data = message.get("type"), message.get("data")
        if not isinstance(data, dict) or data.get("prompt_id") != prompt_id:
            continue  # about something else: other prompts, queue status, custom nodes' own messages
        if kind == "progress" and data.get("node") == progress_node:
            fraction = _fraction(message)
            if on_progress is not None:
                on_progress(fraction)
        elif kind == "execution_error":
            raise StepFailed(format_execution_error(data))
        elif kind == "execution_interrupted":
            raise StepFailed(f"execution_interrupted: {data.get('node_type')} (node {data.get('node_id')})")
        elif kind == "executing" and data.get("node") is None:
            return
    raise StreamEnded(f"ComfyUI's websocket closed before prompt {prompt_id} finished")


def _fraction(message: dict[str, Any]) -> float:
    data = message["data"]
    try:
        fraction = data["value"] / data["max"]
    except (KeyError, TypeError, ZeroDivisionError) as exc:
        raw = json.dumps(message, ensure_ascii=False)[:2000]
        raise UnexpectedResponse("ComfyUI sent a progress message without a usable value / max", raw) from exc
    return min(1.0, max(0.0, fraction))


def format_execution_error(data: dict[str, Any]) -> str:
    """ComfyUI's own words: failing node, exception type and exception message."""
    return f"{data.get('node_type')} (node {data.get('node_id')}): {data.get('exception_type')}: {data.get('exception_message')}"
